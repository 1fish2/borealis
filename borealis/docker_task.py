"""A Firetask that runs a shell command in a Docker container, pulling input
files from Google Cloud Storage (GCS) and pushing output files to GCS.
"""

from __future__ import absolute_import, division, print_function

from collections import namedtuple
import logging
import os
from pprint import pformat
import shutil
from threading import Event, Timer
import time
from typing import Any, List, Optional

import docker
from docker import errors as docker_errors
from docker.models.containers import Container
from docker.types import Mount
from docker.utils import parse_repository_tag
from fireworks import explicit_serialize, FiretaskBase, FWAction
import requests

from borealis.util import data
import borealis.util.filepath as fp
import borealis.util.storage as st


class DockerTaskError(Exception):
    """An error in DockerTask setup, cleanup, or running the Docker payload."""
    pass


PathMapping = namedtuple('PathMapping', 'captures local_prefix local sub_path mount')


try:
    seconds_clock = time.monotonic
except AttributeError:
    # This clock works in Python 2 but it goes down if the system clock gets set back.
    seconds_clock = time.time


def uid_gid():
    """Return the Unix uid:gid (user ID, group ID) pair."""
    return '{}:{}'.format(fp.run_cmdline('id -u'), fp.run_cmdline('id -g'))


def captures(path):
    # type: (str) -> Optional[str]
    """Categorize the given output path as capturing a log (>>), capturing
    just stdout + stderr (>), or neither (a regular file or directory).
    """
    return ('>>' if path.startswith('>>')
            else '>' if path.startswith('>')
            else None)


@explicit_serialize
class DockerTask(FiretaskBase):
    """
    Firetask Parameters
    -------------------
    name: the payload task name, for logging.

    image: the Docker Image to pull, e.g. 'gcr.io/MY-GCLOUD-PROJECT/MY-CODE'.
      You can put a ':TAG' on it but keep in mind that ':latest' has nothing to
      do with time. It's merely the default Docker tag name.

    command: the shell command tokens to run in the Docker Container.

    internal_prefix: the base pathname inside the Docker Container for inputs
      and outputs (files and directory trees).

    storage_prefix: the GCS base pathname for inputs and outputs.

    inputs, outputs: absolute pathnames internal to the Docker container of
      input/output files and directories to pull/push to GCS. DockerTask will
      construct the corresponding GCS storage paths by rebasing each path from
      `internal_prefix` to `storage_prefix`, and the corresponding local paths
      outside the container by rebasing each path to `local_prefix`.

      NOTE: Each path in `inputs` and `outputs` names a directory tree of
      files if it ends with '/', otherwise a file. This is needed because the
      GCS is a flat object store without directories and DockerTask needs to
      know whether to create files or directories.

      If the task completes normally, DockerTask will all its outputs to GCS.
      Otherwise, it only writes '>>' log files.

      An output path that starts with '>>' will capture a log of stdout +
      stderr + other log messages like elapsed time and task exit code.
      DockerTask will write it even if the task fails (to aid debugging),
      unlike all the other outputs.

      An output path that starts with '>' captures stdout + stderr. The rest
      of the path is as if internal to the container, and will get rebased to
      to compute its storage path.

    timeout: in seconds, indicates how long to let the task run.
    """

    _fw_name = 'DockerTask'
    DEFAULT_TIMEOUT_SECONDS = 60 * 60

    required_params = [
        'name',
        'image',
        'command',
        'internal_prefix',
        'storage_prefix']
    optional_params = [
        'inputs',
        'outputs',
        'timeout']

    LOCAL_BASEDIR = os.path.join(os.sep, 'tmp', 'fireworker')

    def _log(self):
        # type: () -> logging.Logger
        """Return a Logger for this task."""
        parent = logging.getLogger('dockerfiretask')
        parent.setLevel(logging.DEBUG)

        name = 'dockerfiretask.{}'.format(self['name'])
        return logging.getLogger(name)

    def pull_docker_image(self, docker_client):
        # type: (docker.DockerClient) -> Any  # a Docker Image
        """Pull the requested Docker Image. Ensure there's a tag so pull() will
        get one Image rather than all tags in a repository.
        """
        repository, tag = parse_repository_tag(self['image'])
        if not tag:
            tag = 'latest'  # 'latest' is the default tag; it doesn't mean squat
        self._log().info('Pulling Docker image %s:%s', repository, tag)
        try:
            image = docker_client.images.pull(repository, tag)
            self._log().debug('Pulled Docker image %s', image.id)
        except requests.ConnectionError as e:
            raise DockerTaskError(
                "Couldn't connect to the Docker server. You might need to"
                " install one or start it. {!r}".format(e))
        return image

    def rebase(self, internal_path, new_prefix):
        # type: (str, str) -> str
        """Strip off any '>' or '>>' prefix then rebase the internal-to-container
        path from the internal_prefix to new_prefix.

        '>' or '>>' means capture stdout + stderr rather than fetching a
        container file; the rest of internal_path is the as-if-internal path.
        '>>' creates a log of stdout + stderr + additional information and
        writes it even if the Docker command fails.

        A path ending with '/' indicates a directory.
        """
        core_path = internal_path.lstrip('>')
        internal_prefix = self['internal_prefix']
        rel_path = st.relpath(core_path, internal_prefix)
        new_path = os.path.join(new_prefix, rel_path)

        if '..' in new_path:
            # This could happen if `internal_path` doesn't start with
            # `internal_prefix`.
            raise DockerTaskError(
                'Rebased storage I/O path "{}" contains ".."'.format(new_path))
        return new_path

    def setup_mount(self, internal_path, local_prefix):
        # type: (str, str) -> PathMapping
        """Create a PathMapping between a path internal to the Docker container
        and a sub_path relative to the storage_prefix (GCS) and to the local_prefix
        (local file system), make the Docker local:internal Mount object, and
        create the local file or directory so Docker can detect whether to mount
        a file or directory.

        Timestamp log filenames to preserve the run history and improve alpha
        sorting.
        """
        caps = captures(internal_path)

        if caps:
            sub_dir, filename = os.path.split(internal_path)
            if not filename:
                raise DockerTaskError(
                    'A stdout capture path must name a file, not a directory: "{}"'
                        .format(internal_path))
            if caps == '>>':
                filename = '{}_{}'.format(data.timestamp(), filename)
                internal_path = os.path.join(sub_dir, filename)

        local_path = self.rebase(internal_path, local_prefix)
        sub_path = self.rebase(internal_path, '')

        fp.makedirs(os.path.dirname(local_path))
        if not st.names_a_directory(local_path):
            open(local_path, 'a').close()

        # Create the Docker Mount unless this mapping will capture stdout & stderr.
        mount = (None if caps
                 else Mount(target=internal_path, source=local_path, type='bind'))

        return PathMapping(caps, local_prefix, local_path, sub_path, mount)

    def setup_mounts(self, group):
        # type: (str) -> List[PathMapping]
        """Set up all the mounts for the 'inputs' or 'outputs' group."""
        group_base_dir = os.path.join(self.LOCAL_BASEDIR, group)
        return [self.setup_mount(path, group_base_dir)
                for path in self.get(group, [])]

    def _outputs_to_push(self, lines, success, outs, prologue, epilogue):
        # type: (List[str], bool, List[PathMapping], str, str) -> List[PathMapping]
        """Write requested stdout+stderr and log output files, then return a
        list of output PathMappings to push to GCS: all of them if the Task
        succeeded; only the '>>' logs if it failed.
        """
        to_push = []

        for out in outs:
            if out.captures:
                try:
                    with open(out.local, 'w') as f:
                        hr = ''
                        if out.captures == '>>':
                            hr = '-' * 80
                            f.write('{}\n\n{}\n'.format(prologue, hr))

                        f.writelines(lines)

                        if hr:
                            f.write('{}\n\n{}\n'.format(hr, epilogue))
                except IOError as _:
                    self._log().exception('Error capturing to %s', out.local)

            if success or out.captures == '>>':
                to_push.append(out)

        return to_push

    def push_to_gcs(self, to_push):
        # type: (List[PathMapping]) -> bool
        """Push outputs to GCS. Return True if successful."""
        ok = True
        prefix = self['storage_prefix']

        self._log().info('Pushing %s outputs to GCS %s: %s',
            len(to_push), prefix, [mapping.sub_path for mapping in to_push])
        gcs = st.CloudStorage(prefix)

        for mapping in to_push:
           ok = gcs.upload_tree(mapping.local, mapping.sub_path) and ok

        return ok

    def pull_from_gcs(self, to_pull):
        # type: (List[PathMapping]) -> bool
        """Pull inputs from GCS. Return True if successful."""
        ok = True
        prefix = self['storage_prefix']

        self._log().info('Pulling %s inputs from GCS %s: %s',
            len(to_pull), prefix, [mapping.sub_path for mapping in to_pull])
        gcs = st.CloudStorage(prefix)

        for mapping in to_pull:
            ok = gcs.download_tree(mapping.sub_path, mapping.local_prefix) and ok

        return ok

    def _terminate(self, container, logger, reason, terminated):
        # type: (Container, logging.Logger, str, Event) -> None
        """Terminate the Docker Container's process.

        This runs in a Timer thread so be careful about mutable state: Signal
        that termination happened using an Event object and cope if the
        Container already stopped. But this relies on thread-safety in Logger
        and the Docker client.

        NOTE: "The KeyboardInterrupt exception will be received by an arbitrary
        thread." -- https://docs.python.org/3.8/library/_thread.html
        """
        name = self['name']
        logger.info('Terminating task {} for {}...'.format(name, reason))

        try:
            container.stop()
            terminated.set()
            logger.warning('Terminated task {} for {}'.format(name, reason))
        except docker_errors.APIError as e:
            logger.warning("Couldn't terminate task {} for {}: {}".format(
                name, reason, e))


    # ODDITIES ABOUT THE PYTHON DOCKER PACKAGE
    #
    # images.pull() will pull a list of images if neither arg gives a tag. We
    # don't want that, so provide a default tag if the image name doesn't
    # include one.
    #
    # Some variations on the call sequence bury the exit code and stdout, so
    # test carefully when changing it and remove the container manually.
    #
    # Mounts need to be set up just right. Use type='bind' and pre-create the
    # local file or directory.
    #
    # Example container.attrs['State'] (if it is a dict):
    #   {'Dead': False,
    #    'Error': '',
    #    'ExitCode': 0,        # always seems to be 0!
    #    'FinishedAt': '0001-01-01T00:00:00Z',
    #    'OOMKilled': False,
    #    'Paused': False,
    #    'Pid': 0,
    #    'Restarting': False,
    #    'Running': False,
    #    'StartedAt': '0001-01-01T00:00:00Z',
    #    'Status': 'created'}  # always seems to be 'created'!
    #
    # Examples of container.wait() results:
    #   {'Error': None, 'StatusCode': 0}
    #   {'Error': None, 'StatusCode': 1}

    def run_task(self, fw_spec):
        # type: (dict) -> Optional[FWAction]
        """Run a task as a shell command in a Docker container."""
        start_timestamp = data.timestamp()
        name = self['name']
        errors = []  # type: List[str]
        lines = []  # type: List[str]
        image = None
        timeout = self.get('timeout', self.DEFAULT_TIMEOUT_SECONDS)
        elapsed = '---'
        logger = self._log()

        def check(success, or_error):
            if not success:
                errors.append(or_error)

        def prologue():
            return ('{} DockerTask {}\n\n'
                    '{}\n\n'
                    'Docker Image ID: {}').format(
                start_timestamp,
                name,
                pformat(self.to_dict()),
                image.id if image else '---')

        def epilogue():
            return '{} task: {}, elapsed {} of timeout parameter {} {}'.format(
                'FAILED' if errors else 'SUCCESSFUL',
                name,
                elapsed,
                data.format_duration(timeout),
                errors if errors else '')

        logger.warning('STARTING TASK: %s', name)

        try:
            docker_client = docker.from_env()
            image = self.pull_docker_image(docker_client)

            ins = self.setup_mounts('inputs')
            outs = self.setup_mounts('outputs')

            check(self.pull_from_gcs(ins), 'Failed to fetch inputs from GCS')

            # -----------------------------------------------------
            logger.info('Running: %s', self['command'])
            mounts = [mapping.mount for mapping in ins + outs if mapping.mount]
            start_secs = seconds_clock()
            container = docker_client.containers.run(
                image,
                command=self['command'],
                user=uid_gid(),
                mounts=mounts,
                detach=True)  # type: Container

            try:
                terminated = Event()
                args = (container, logger, 'timeout', terminated)
                timer = Timer(timeout, self._terminate, args=args)
                timer.start()

                try:
                    for line in container.logs(stream=True):
                        line = line.decode()
                        lines.append(line)
                        logger.info('%s', line.rstrip())
                finally:
                    timer.cancel()

                end_seconds = seconds_clock()
                exit_code = container.wait(timeout=10)['StatusCode']
                elapsed = data.format_duration(end_seconds - start_secs)
                # -----------------------------------------------------

                check(not terminated.is_set(), 'Docker process timeout')
                check(exit_code == 0, 'Docker process exit code {}{}'.format(
                    exit_code, ' (SIGKILL)' if exit_code == 137 else ''))
                container.reload()  # query the Docker daemon for current attrs
                state = container.attrs.get('State')
                if isinstance(state, dict):
                    check(not state.get('OOMKilled'),
                          'The Docker process ran out of memory (OOMKilled)')
            finally:
                try:
                    container.remove(force=True)
                except docker_errors.APIError as _:  # troubling but not a task error
                    logger.exception('Error removing the Docker Container')

            to_push = self._outputs_to_push(
                lines, not errors, outs, prologue(), epilogue())

            # NOTE: The >>task.log file won't report push failures since it's
            # written before pushing and might itself fail to push. But the
            # StackDriver log will get it.
            check(self.push_to_gcs(to_push), 'Failed to store outputs to GCS')

        except (Exception, KeyboardInterrupt) as e:
            # Log it, clean up, and re-raise it. That'll FIZZLE the Firework.
            check(False, repr(e))
            raise
        finally:
            logger.warning('%s', epilogue())

            # [Could wipe just os.path.join(self.LOCAL_BASEDIR, 'inputs') to
            # keep the outputs for local scrutiny.]
            wipe_out = self.LOCAL_BASEDIR
            shutil.rmtree(wipe_out, ignore_errors=True)

        if errors:
            raise DockerTaskError(repr(errors))  # FIZZLE this Firework.

        return None
