#!/usr/bin/env python
"""A Fireworks worker on Google Compute Engine to "rapidfire" launch rockets.

NOTE: When running as a systemd service or otherwise outside an interactive
console, set the `PYTHONUNBUFFERED=1` environment variable or run with
`python -u fireworker.py` so the logging output comes out in real time rather
than buffering up into long delayed chunks.
"""

from __future__ import absolute_import, division, print_function

import argparse
import logging
import socket
import sys
import time

from fireworks import LaunchPad, FWorker
from fireworks.core import rocket_launcher
import google.cloud.logging as gcl
import ruamel.yaml as yaml
from typing import Any, Dict

from cloud import gcp



#: The standard launchpad config filename. Read it and override some fields.
LAUNCHPAD_FILE = 'my_launchpad.yaml'

#: Fireworker logger.
FW_LOGGER = logging.getLogger('fireworker')
FW_LOGGER.setLevel(logging.DEBUG)

#: Fireworker console-only logger.
FW_CONSOLE_LOGGER = logging.getLogger('fireworker.console')
FW_CONSOLE_LOGGER.setLevel(logging.DEBUG)
FW_CONSOLE_LOGGER.propagate = False


def setup_logging(instance_name):
    # type: (str) -> None
    """Set up GCP StackDriver logging for the given GCE instance name, if
    running on GCE with Python's root logger.
    """
    # TODO(jerry): Set the StackDriver resource type (GCE VM) and name.

    for log_name in ('launchpad', 'rocket.launcher'):
        logging.getLogger(log_name).setLevel('INFO')

    # TODO(jerry): Don't enable StackDriver logging when running locally (or
    #  limit it to WARNING+ level) to reduce cost and quota usage.
    # if instance_name: ...
    client = gcl.Client()
    exclude = (FW_CONSOLE_LOGGER.name, 'docker', 'urllib3')
    client.setup_logging(log_level=logging.WARNING, excluded_loggers=exclude)


class Fireworker(object):

    def __init__(self, lpad_config, host_name):
        # type: (Dict[str, Any], str) -> None
        self.lpad_config = lpad_config
        self.strm_lvl = lpad_config.get('strm_lvl') or 'INFO'
        self.host_name = host_name

        self.sleep_secs = 10
        self.idle_for_waiters = 60 * 60
        self.idle_for_queued = 15 * 60  # TODO(jerry): Rename this

        self.launchpad = LaunchPad(**lpad_config)

        # Can optionally set a specific `category` of jobs to pull, a `query`
        # to restrict the type of Fireworks to run, and an `env` to pass
        # worker-specific into to the Firetasks.
        self.fireworker = FWorker(host_name)

    def launch_rockets(self):
        # type: () -> None
        """Keep launching rockets that are ready to go. Stop after:
          * idling idle_for_waiters secs for WAITING rockets to become ready,
          * idling idle_for_queued secs if no rockets are even waiting,
          * the custom metadata field `attributes/quit` becomes 'when-idle'.

        The first timeout should be long enough to wait around to run queued
        rockets after running rockets finish prerequisite work. The second
        timeout should be long enough to let new work get queued.
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
                future_work = self.launchpad.future_run_exists(self.fireworker)  # any waiting?
                if idled >= (self.idle_for_waiters if future_work else self.idle_for_queued):
                    return

                if gcp.instance_metadata('attributes/quit') == 'when-idle':
                    FW_LOGGER.info('Quitting by "when-idle" request')
                    return

                FW_CONSOLE_LOGGER.info(
                    'Sleeping for %s secs waiting for launchable rockets',
                    self.sleep_secs)
                time.sleep(self.sleep_secs)
                idled += self.sleep_secs


class Redacted(object):
    def __repr__(self):
        return '*****'


def main(development=False):
    # type: (bool) -> None
    """Run as a FireWorks worker node on Google Compute Engine (GCE), launching
    Fireworks rockets in rapidfire mode then deleting this GCE VM instance.

    Get configuration settings from GCE VM metadata fields:
        name - the Fireworker name [required]
        attributes/db - DB name (user-specific or workflow-specific) [required]
        attributes/username - DB username [optional]
        attributes/password - DB password [optional]
    secondarily from my_launchpad.yaml:
        host, port - for the DB connection [required]
        logdir, strm_lvl, ... [optional]
        DB name, DB username, and DB password [fallback]
    with fallbacks:
        name - 'fireworker'
        DB name - 'default_fireworks_database'
        DB username, DB password - null
        logdir - './logs/worker' (my_launchpad takes precedence, even if null)
        strm_lvl - 'INFO'

    The DB username and password are needed if MongoDB is set up to require
    authentication, and it could use shared or user-specific accounts.

    TODO: Add configuration settings for idle_for_waiters and idle_for_queued.

    You can set a custom metadata field to make this worker stop idling:
        gcloud compute instances add-metadata INSTANCE-NAME --metadata quit=when-idle
    """
    exit_code = 1

    try:
        instance_name = gcp.gce_instance_name()
        host_name = instance_name or socket.gethostname()
        setup_logging(instance_name)

        with open(LAUNCHPAD_FILE) as f:
            lpad_config = yaml.safe_load(f)  # type: dict

        db_name = (gcp.instance_metadata('attributes/db')
                   or lpad_config.get('name', 'default_fireworks_database'))
        lpad_config['name'] = db_name

        username = (gcp.instance_metadata('attributes/username')
                    or lpad_config.get('username'))
        password = (gcp.instance_metadata('attributes/password')
                    or lpad_config.get('password'))
        lpad_config['username'] = username
        lpad_config['password'] = password

        redacted_config = dict(lpad_config, password=Redacted())
        FW_LOGGER.warning(
            '\nStarting Fireworker on %s with LaunchPad config: %s\n',
            host_name, redacted_config)

        fireworker = Fireworker(lpad_config, host_name)
        fireworker.launch_rockets()

        exit_code = 0
    except KeyboardInterrupt:
        FW_LOGGER.warning('KeyboardInterrupt -- exiting')
        sys.exit(2)
    except Exception:
        FW_LOGGER.exception('Fireworker error')

    shut_down(development, exit_code)


def shut_down(development, exit_code):
    # type: (bool, int) -> None
    """Shut down this program or this entire GCE VM (if running on GCE and not
    `development`).
    """
    if development:
        sys.exit(exit_code)
    else:
        if exit_code:  # an unexpected failure, e.g. missing a needed pip
            FW_LOGGER.warning(
                'Delaying before deleting this GCE VM to allow some time to'
                ' connect to it and stop this service so you can fix the problem'
                ' and make a new Disk Image.')
            time.sleep(15 * 60)

        FW_LOGGER.warning("Fireworker shutting down.")
        gcp.delete_this_vm(exit_code)


def cli():
    parser = argparse.ArgumentParser(
        description='Run as a FireWorks worker node, launching rockets rapidfire.'
                    ' Designed for Google Compute Engine (GCE).'
                    ' Gets configuration settings from GCE and my_launchpad.yaml,'
                    ' with fallbacks.')
    parser.add_argument(
        '--development', action='store_true',
        help="Development mode: When done, just exit Python without deleting"
             " this GCE VM worker instance (if running on GCE).")

    args = parser.parse_args()
    main(development=args.development)


if __name__ == '__main__':
    cli()
