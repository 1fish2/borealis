"""Google Cloud Platform utilities."""

from __future__ import absolute_import, division, print_function

import requests
import sys
from typing import Optional

from util import filepath as fp


def gcloud_get_config(section_property):
    # type: (str) -> str
    """Get a "section/property" configuration value from the gcloud command line
    tool. Raise an exception if the value is not configured (`gcloud` status
    code error) or if `gcloud` isn't installed.
    """
    return fp.run_cmd(['gcloud', 'config', 'get-value', str(section_property)])


def project():
    # type: () -> str
    """Get the current Google Cloud Platform (GCP) project. This works both
    on and off of Google Cloud as long as the `gcloud` command line tool was
    configured."""
    return gcloud_get_config('core/project')


def zone(complain_off_gcp=True):
    # type: (bool) -> str
    """Get the current Google Compute Platform (GCP) zone from the metadata
    server when running on Google Cloud, else from the `gcloud` command line tool.
    """
    zone_metadata = instance_metadata(
        'zone', '', complain_off_gcp=complain_off_gcp).split('/')[-1]
    return zone_metadata or gcloud_get_config('compute/zone')


def instance_metadata(field, default=None, complain_off_gcp=True):
    # type: (str, str, bool) -> Optional[str]
    """Get a metadata field like the "name", "zone", or "attributes/db" (for
    custom metadata field "db") of this Google Compute Engine VM instance from
    the GCP metadata server. On a ConnectionError (when not running on Google
    Cloud), print a message if `complain_off_gcp`, then return `default`.

    "attributes/*" metadata fields can be set when creating a GCE instance:
    `gcloud compute instances create worker --metadata db=fred ...`
    They can be set or changed on a running instance:
    `gcloud compute instances add-metadata instance-name --metadata db=ginger`
    """
    url = "http://metadata.google.internal/computeMetadata/v1/instance/{}".format(field)
    headers = {'Metadata-Flavor': 'Google'}
    timeout = 5  # seconds

    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        return r.text if r.status_code == 200 else default
    except requests.exceptions.RequestException as e:
        if complain_off_gcp:
            print('''Note: Couldn't connect to the GCP Metadata server to get "{}".'''
                  .format(field))
        return default


def gce_instance_name():
    # type: () -> str
    """Return this GCE VM instance name if running on GCE, or None if not
    running on GCE.
    """
    return instance_metadata('name')


def delete_this_vm():
    # type: () -> None
    """Ask gcloud to delete this GCE VM instance if running on GCE. In any case
    exit Python if not already shut down, and Python cleanup actions might run.
    """
    name = gce_instance_name()

    if name:
        print('Deleting GCE VM "{}"...'.format(name))
        my_zone = zone()
        fp.run_cmd(['gcloud', '--quiet', 'compute', 'instances', 'delete',
                    name, '--zone', my_zone])
    else:
        print('Exiting (not running on GCE).')

    sys.exit()
