#!/usr/bin/env python
"""A Fireworks worker on Google Compute Engine to "rapidfire" launch rockets.

    python -m borealis.fireworker

The borealis-fireworker setup.py installs console_scripts for fireworker and
gce so with that you can simply run

    fireworker

NOTE: When running as a systemd service or otherwise outside an interactive
console, set the `PYTHONUNBUFFERED=1` environment variable or run with
`python -u fireworker.py` so the logging output comes out in real time rather
than buffering up into long delayed chunks.
"""

from __future__ import absolute_import, division, print_function

import argparse
import logging
import os
import socket
import sys
import time
from typing import Any, Dict, Optional

from fireworks import LaunchPad, FWorker, fw_config
from fireworks.core import rocket_launcher
import google.cloud.logging as gcl
from google.cloud.logging.resource import Resource
import ruamel.yaml as yaml

from borealis.util import gcp
from borealis.util.log_filter import LogPrefixFilter

#: The default launchpad config filename (in CWD) to read.
#: GCE instance metadata will override some field values.
DEFAULT_LPAD_YAML = 'my_launchpad.yaml'
DEFAULT_FIREWORKS_DATABASE = 'default_fireworks_database'
DEFAULT_IDLE_FOR_WAITERS = 60 * 60  # seconds
DEFAULT_IDLE_FOR_ROCKETS = 15 * 60  # seconds

ERROR_EXIT_CODE = 1
KEYBOARD_INTERRUPT_EXIT_CODE = 2

#: Fireworker logger.
FW_LOGGER = logging.getLogger('fireworker')
FW_LOGGER.setLevel(logging.DEBUG)

#: Fireworker console-only logger.
FW_CONSOLE_LOGGER = logging.getLogger('fireworker.console')
FW_CONSOLE_LOGGER.setLevel(logging.DEBUG)
FW_CONSOLE_LOGGER.propagate = False


def _setup_logging(gce_instance_name, host_name):
    # type: (str, str) -> None
    """Set up GCP StackDriver cloud logging on Python's root logger for the GCE
    instance name or any host name. Set a narrow logging filter if running off
    GCE (instance_name is empty).
    """
    exclude = (FW_CONSOLE_LOGGER.name, 'urllib3')

    monitored_resource = Resource(
        type='gce_instance',
        labels={  # Add a 'tag' label? It gets 'project_id' automatically.
            'instance_id': host_name,
            'zone': gcp.zone()})
    client = gcl.Client()

    # noinspection PyTypeChecker
    client.setup_logging(
        log_level=logging.WARNING,
        excluded_loggers=exclude,
        name=FW_LOGGER.name,
        resource=monitored_resource)

    # To StackDriver cloud logs (which aggregate all machines): From workers
    # running "locally" (off GCE), log at the WARNING level including start/end
    # messages (which should be at the NOTICE level but Logs Viewer is unhelpful
    # with NOTICE). That filters out the 'dockerfiretask' payload stdout lines.
    # From workers on GCE, log at the DEBUG level to enable remote debugging.
    # Set the Logs Viewer to INFO level for more conciseness.
    #
    # To console logs: Filter out messages already printed by handlers on nested
    # loggers "launchpad" and "rocket.launcher", allowing WARNINGs just in case.
    root = logging.getLogger()
    fworker_level = logging.DEBUG if gce_instance_name else logging.WARNING
    cloud_filter = LogPrefixFilter(
        {'fireworker': fworker_level, 'dockerfiretask': fworker_level},
        logging.WARNING)
    console_filter = LogPrefixFilter(
        {'fireworker': logging.INFO, 'dockerfiretask': logging.DEBUG},
        logging.WARNING)
    for handler in root.handlers:
        # This `is_cloud` test is a bit fragile.
        is_cloud = hasattr(handler, 'transport') or hasattr(handler, 'resource')
        handler.addFilter(cloud_filter if is_cloud else console_filter)


def _cleanup_logging():
    # type: () -> None
    """Clean up StackDriver cloud logging: Flush and remove root logger's
    background-transport handlers so the last messages get to the server and
    won't raise RuntimeError('cannot schedule new futures after shutdown').

    StackDriver should be out of the loop after this but there's no documented
    API for this so hopefully it's right, idempotent, and safe if StackDriver
    logging was not set up.
    """
    root = logging.getLogger()

    for handler in list(root.handlers):
        if hasattr(handler, 'transport'):
            transport = handler.transport
            if hasattr(transport, 'flush'):
                transport.flush()
                root.removeHandler(handler)


class Fireworker(object):
    """A Fireworks worker on Google Compute Engine to "rapidfire" launch rockets.

    NOTE: When running as a systemd service or otherwise outside an interactive
    console, set the `PYTHONUNBUFFERED=1` environment variable or run with
    `python -u fireworker.py` so the logging output comes out in real time rather
    than buffering up into long delayed chunks.
    """

    def __init__(self, lpad_config, host_name):
        # type: (Dict[str, Any], str) -> None
        """
        :param lpad_config: LaunchPad() configuration parameters *and*
            idle_for_waiters: see launch_rockets(), default = 60 minutes;
            idle_for_rockets: see launch_rockets(), default = 15 minutes
        :param host_name: this network host name
        """
        self.lpad_config = lpad_config.copy()
        self.host_name = host_name

        # NOTE: FireWorks creates loggers with stdout stream handlers for each
        # (name, level) pair. So setting strm_lvl='WARNING' gets both INFO and
        # WARNING handlers which might print duplicate lines. Try to tame it.
        self.strm_lvl = lpad_config.get('strm_lvl') or 'INFO'
        fw_config.ROCKET_STREAM_LOGLEVEL = self.strm_lvl

        self.sleep_secs = 10
        self.idle_for_rockets = int(lpad_config.pop('idle_for_rockets', DEFAULT_IDLE_FOR_ROCKETS))
        self.idle_for_waiters = max(
            int(lpad_config.pop('idle_for_waiters', DEFAULT_IDLE_FOR_WAITERS)),
            self.idle_for_rockets)

        self.launchpad = LaunchPad(**lpad_config)
        self.launchpad.m_logger.setLevel(self.strm_lvl)  # set non-stream level

        # Can optionally set a specific `category` of jobs to pull, a `query`
        # to restrict the type of Fireworks to run, and an `env` to pass
        # worker-specific into to the Firetasks.
        self.fireworker = FWorker(host_name)

    def launch_rockets(self):
        # type: () -> str
        """Keep launching rockets that are ready to go. Stop after:
          * idling idle_for_rockets secs for any rockets READY to run (default
            15 minutes),
          * idling idle_for_waiters secs for WAITING rockets to become READY
            (for queued rockets that are waiting on other rockets; default 60
            minutes; >= idle_for_rockets),
          * while idling, the custom metadata attribute `quit` got set
            (gcloud compute instances add-metadata...) to 'soon' or 'when-idle'
          * between rockets, the custom metadata attribute `quit` got set to
            'soon'

        Returns the stop reason.
        """
        # rapidfire() launches READY rockets until: `max_loops` batches of READY
        # rockets OR `timeout` total elapsed seconds OR `nlaunches` rockets launched
        # OR `nlaunches` == 0 ("until completion", the default) AND no rockets are
        # even waiting.
        #
        # Set max_loops so it won't loop forever and we can track idle time.
        #
        # TODO(jerry): Set m_dir? local_redirect?
        while True:
            rocket_launcher.rapidfire(
                self.launchpad, self.fireworker, strm_lvl=self.strm_lvl,
                max_loops=1, sleep_time=self.sleep_secs)

            # Idle to the max.
            idled = self.sleep_secs  # rapidfire() just slept once
            while not self.launchpad.run_exists(self.fireworker):  # none ready to run
                future_work = self.launchpad.future_run_exists(self.fireworker)  # any ready or waiting?
                if idled >= (self.idle_for_waiters if future_work else self.idle_for_rockets):
                    return 'idle'

                req = gcp.instance_attribute('quit')
                if req == 'soon' or req == 'when-idle':
                    return '"quit={}" request'.format(req)

                FW_CONSOLE_LOGGER.info(
                    'Sleeping for %s secs waiting for launchable rockets',
                    self.sleep_secs)
                time.sleep(self.sleep_secs)
                idled += self.sleep_secs

            req = gcp.instance_attribute('quit')
            if req == 'soon':
                return '"quit={}" request'.format(req)


class Redacted(object):
    def __repr__(self):
        """Print without quotes to look like a mask not a lame password."""
        return '*****'


def main(development=False, launchpad_filename=DEFAULT_LPAD_YAML):
    # type: (bool, str) -> None
    """Run as a FireWorks worker node on Google Compute Engine (GCE), launching
    Fireworks rockets in rapidfire mode then deleting this GCE VM instance.

    Get initialization configuration settings from GCE VM metadata fields (when
    on GCE):
        name - the Fireworker name
        attributes/db - DB name (user-specific or workflow-specific)
        attributes/username - DB username
        attributes/password - DB password
        attributes/idle_for_rockets - idle this many seconds for any rockets
            READY to run (default 15 minutes)
        attributes/idle_for_waiters - idle this many seconds for WAITING rockets
            to become READY (for queued rockets that are waiting on other
            rockets; default 60 minutes; >= idle_for_rockets)
    else from the launchpad yaml file named by the `launchpad_filename` arg:
        DB host, DB port - for the MongoDB connection
        DB name
        DB username, DB password - null for no user authentication
        logdir, strm_lvl, ... - for "launchpad" & "rocket" logging
        idle_for_waiters, idle_for_rockets
    with fallbacks:
        name - the network hostname
        DB host, DB port - localhost:27017 (Fireworks defaults)
        DB name - DEFAULT_FIREWORKS_DATABASE
        DB username, DB password - null
        logdir, strm_lvl - FireWorks defaults
        idle_for_waiters, idle_for_rockets - see Fireworker()

    The DB username and password are needed if MongoDB is set up to require
    authentication, and it could use shared or user-specific accounts.

    While running, you can set a custom metadata field to make this worker stop
    idling:
        gcloud compute instances add-metadata INSTANCE-NAME --metadata quit=when-idle
    or stop as soon as it finishes the current rocket:
        gcloud compute instances add-metadata INSTANCE-NAME --metadata quit=soon
    """
    def metadata_else_config(attribute, default=None, config_key=None):
        # type: (str, Any, Optional[str]) -> Any
        """Put a GCE metadata attribute, or else a keyed `lpad_config` value
        (`config_key` defaults to `attribute`), or else the default into
        `lpad_config[config_key]`.
        Attributes are always strings. They can be absent but they can't be
        `None` or a number, so treat '' like absent.
        Config values can be `None` (`null` in YAML) or a number, so let any
        value override the default.
        """
        config_key = config_key or attribute
        value = (gcp.instance_attribute(attribute)
                 or lpad_config.get(config_key, default))
        lpad_config[config_key] = value

    exit_code = ERROR_EXIT_CODE

    try:
        instance_name = gcp.gce_instance_name()
        host_name = instance_name or socket.gethostname()
        _setup_logging(instance_name, host_name)

        FW_CONSOLE_LOGGER.info('Reading launchpad config "{}"'.format(
            launchpad_filename))
        with open(launchpad_filename) as f:
            lpad_config = yaml.safe_load(f)  # type: dict

        metadata_else_config('db', DEFAULT_FIREWORKS_DATABASE, 'name')
        metadata_else_config('username')
        metadata_else_config('password')
        metadata_else_config('idle_for_waiters', DEFAULT_IDLE_FOR_WAITERS)
        metadata_else_config('idle_for_rockets', DEFAULT_IDLE_FOR_ROCKETS)

        redacted_config = dict(lpad_config, password=Redacted())
        FW_LOGGER.warning(
            '\nStarting Fireworker on %s with LaunchPad config: %s\n',
            host_name, redacted_config)

        fireworker = Fireworker(lpad_config, host_name)
        stop_reason = fireworker.launch_rockets()
        FW_LOGGER.warning('Fireworker -- normal exit: {}'.format(stop_reason))
        exit_code = 0
    except KeyboardInterrupt:
        FW_LOGGER.warning('Fireworker -- KeyboardInterrupt exit')
        exit_code = KEYBOARD_INTERRUPT_EXIT_CODE
    except Exception as e:
        FW_LOGGER.exception('Fireworker -- error exit: {}'.format(e))

    _cleanup_logging()
    _shut_down(development, exit_code)


def _shut_down(development, exit_code):
    # type: (bool, int) -> None
    """Shut down this program or this entire GCE VM (if running on GCE and not
    `development` and `exit_code` isn't KEYBOARD_INTERRUPT_EXIT_CODE).
    """
    if development or exit_code == KEYBOARD_INTERRUPT_EXIT_CODE:
        sys.exit(exit_code)
    else:
        if exit_code:  # an unexpected failure, e.g. missing a needed pip
            FW_CONSOLE_LOGGER.warning(
                'Delaying before deleting this GCE VM to allow some time to'
                ' connect to it and stop this service so you can fix the problem'
                ' and make a new Disk Image.')
            time.sleep(15 * 60)

        gcp.delete_this_vm(exit_code)


def cli():
    """Command Line Interpreter to run a Fireworker."""
    pkg_dir = os.path.dirname(__file__)
    setup_dir = os.path.join(pkg_dir, 'setup')

    parser = argparse.ArgumentParser(
        description=
            'Run as a FireWorks worker node, launching rockets rapidfire.'
            ' Designed for Google Compute Engine (GCE) and Google Cloud Storage'
            ' (GCS). Gets configuration settings from GCE metadata attributes'
            ' (when running on GCE) and from the Launchpad file (see the `-l`'
            ' option), with fallbacks.'
            ' The setup source files are "{}/*"'.format(setup_dir))
    parser.add_argument('-l', dest='launchpad_filename',
        default=DEFAULT_LPAD_YAML,
        help='Launchpad config YAML filename (default="{}").'.format(
            DEFAULT_LPAD_YAML))
    parser.add_argument('-s', '--setup', action='store_true',
        help='Print the path containing the setup files, then'
             ' exit. Try: `SETUP=$(fireworker --setup)`')
    parser.add_argument('--development', action='store_true',
        help="Development mode: When done, just exit Python without deleting"
             " this GCE VM worker instance (if running on GCE).")

    args = parser.parse_args()
    if args.setup:
        print(setup_dir)
        exit(0)

    main(development=args.development, launchpad_filename=args.launchpad_filename)


if __name__ == '__main__':
    cli()
