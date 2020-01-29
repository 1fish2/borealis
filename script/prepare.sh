#!/usr/bin/env bash
## Instructions to set up a GCE Disk Image for a Borealis Fireworker.
##
## NOTE:
## * The single-# comment lines are instructions to carry out manually.
## * The non-comment lines can be pasted into an ssh shell one section at a
##   time, watching for errors. Some commands will prompt for input.
##
##
# * Create a Google Cloud Platform project if you don't have one already.
#   If needed, enable billing, Compute Engine, Cloud Storage, StackDriver
#   Logging, IAM, Container Registry, and Cloud Build.
# * In the Google Cloud Platform Console > IAM > Service Accounts
#   https://console.cloud.google.com/iam-admin/serviceaccounts create a
#   Service Account "fireworker" and grant it access to your Cloud project.
# * In the Google Cloud Platform Console > IAM
#   https://console.cloud.google.com/iam-admin/iam grant these permissions to
#   your Compute Engine default service account *and* to the fireworker service
#   account:
#       Service Account User
#       Compute Instance Admin v1
#       Logs Writer
#       Storage Object Admin
#       Project Viewer
# * Create a Compute Engine VM instance (to create a Disk Image) using the
#   console https://console.cloud.google.com/compute/instancesAdd or a
#   `gcloud compute instances create` command line.
#     Name: fireworker
#     Region & Zone: <where you want to run everything>
#     Machine family/series: N1 n1-standard-1 [or other. You'll be able to use the
#       resulting Disk Image for any machine type you want.]
#     Boot disk: New 200 GB Standard persistent disk, Ubuntu 19.10 (Not COS)
#       [You can resize it later, but changing the OS image requires creating a
#       new VM from scratch. Container-Optimized OS doesn't have a package
#       manager and is not intended to install software.]
#     Identity and API access: "Compute Engine default service account" or
#       the "fireworker" service account.
#     Access scopes: set access for each API [figure out these details]
#       Cloud Debugger Enabled?
#       Compute Engine Read Write
#       Service Control Enabled
#       Service Management Read Write?
#       Stackdriver Logging Write Only
#       Stackdriver Monitoring Write Only
#       Stackdriver Trace Write Only
#       Storage Read Write
#   Management > Description: Fireworks worker node
#
# * Then access the GCE VM via
#     > gcloud compute ssh fireworker
#   to run the following steps.

sudo apt update
sudo apt upgrade
sudo apt autoremove

sudo apt install -y docker.io
sudo apt install -y make build-essential libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev wget curl llvm libncurses5-dev \
    libncursesw5-dev xz-utils tk-dev libffi-dev liblzma-dev python-openssl git


## Reboot to ensure the apts take effect.
sudo reboot
## Then return.
gcloud compute ssh fireworker


## Reinstall gcloud per https://cloud.google.com/sdk/docs/downloads-interactive
## so it can install updates and the docker-credential-gcr component. (The snap,
## apt, and yum packages don't support that.)
sudo snap remove google-cloud-sdk

curl https://sdk.cloud.google.com > install.sh
sudo mkdir /usr/local/bin/sdk
sudo chgrp ubuntu /usr/local/bin/sdk
sudo chmod g+ws /usr/local/bin/sdk
bash install.sh --install-dir=/usr/local/bin/sdk --disable-prompts

echo '' >> .profile
echo '. /usr/local/bin/sdk/google-cloud-sdk/path.bash.inc' >> .profile
echo '. /usr/local/bin/sdk/google-cloud-sdk/completion.bash.inc' >> .profile
. /usr/local/bin/sdk/google-cloud-sdk/path.bash.inc
. /usr/local/bin/sdk/google-cloud-sdk/completion.bash.inc

echo y | gcloud components install docker-credential-gcr

## Put the SDK's main executables on the path for all users.
sudo ln -s /usr/local/bin/sdk/google-cloud-sdk/bin/gcloud /usr/local/bin/
sudo ln -s /usr/local/bin/sdk/google-cloud-sdk/bin/docker-credential-gcloud /usr/local/bin/
sudo ln -s /usr/local/bin/sdk/google-cloud-sdk/bin/docker-credential-gcr /usr/local/bin/

## Enable docker.
sudo systemctl enable docker
sudo systemctl start docker

## Join the `docker` group so you can run docker commands without `sudo`.
sudo usermod -aG docker $USER
# To make it take effect, disconnect (^D) and `gcloud compute ssh fireworker` back.

## Test docker
docker --version
docker info

## Set up to authenticate to GCR Docker repositories.
## You can pass specific gcr repo names here, e.g. gcr.io,eu.gcr.io,us.gcr.io,asia.gcr.io
gcloud auth configure-docker
## There are alternative ways to authenticate, e.g.:
##   echo y | docker-credential-gcr configure-docker

## Pull any docker images you want to preload for faster DockerTask runs.
docker pull python:2.7.16


## ------------------------------------------------------------------------
## Create and switch to the fireworker user that'll run the borealis service.
sudo adduser --disabled-password fireworker
sudo usermod -aG docker fireworker
sudo su -l fireworker

## Set up fireworker to authenticate to GCR Docker repositories.
gcloud auth configure-docker

## Set up the software environment for borealis-fireworker
curl -L https://github.com/pyenv/pyenv-installer/raw/master/bin/pyenv-installer | bash
{
  echo 'export PATH="$HOME/.pyenv/bin:$PATH"'
  echo 'eval "$(pyenv init -)"'
  echo 'eval "$(pyenv virtualenv-init -)"'; } >> ~/.bash_aliases
source ~/.bash_aliases

git clone https://github.com/CovertLab/borealis.git
cd ~/borealis

pyenv install 3.8.0
pyenv global 3.8.0
pyenv local 3.8.0
pip install --upgrade pip setuptools virtualenv virtualenvwrapper virtualenv-clone wheel
pyenv virtualenv fireworker
pyenv local fireworker
pip install --upgrade pip setuptools virtualenv virtualenvwrapper virtualenv-clone wheel
pip install -r requirements.txt
pyenv rehash

cp example_my_launchpad.yaml my_launchpad.yaml

## Configure fireworker.
# Edit my_launchpad.yaml with connection info for your MongoDB instance and
# optionally set a logdir like /home/fireworker/fw/logs to enable Fireworks logging.

# Logout from the fireworker account (^D).
# ------------------------------------------------------------------------


# Follow the instructions in script/borealis-fireworker.service to set up that
# systemd service.


# Make a disk image in the disk image family "fireworker":
# > sudo shutdown -h now
# * Watch the Compute Engine > VM instances page to see when this VM has fully stopped.
# * Find this disk (e.g. "fireworker") in the Compute Engine > Disks console page.
# * Click "CREATE IMAGE".
# * Name the image like "fireworker-v0", picking the next number in the fireworker series.
# * Set "Family" to "fireworker"  <== MUST DO THIS. The launch-workers script will
#   instantiate workers by Disk Image Family so future images can supersede it.
# * Set "Description" to "Fireworks worker node" or some such to document this Image.
# * Click "Create".
# * When it finishes, delete this GCE VM and its boot disk using gcloud or the console.

# To update this disk image in the future:
# * Start a VM from this disk image, using the console, gcloud, or the gce.py script.
# * `gcloud compute ssh <NAME>` to connect to it.
# * Stop the service:
#     > sudo systemctl stop borealis-fireworker
# * Upgrade apts (sudo apt update && sudo apt upgrade), if you upgraded any or
#   if it printed `*** System restart required ***`: reboot, ssh again, and stop
#   the service again.
# * Pull any Docker images that you want to preload for speed, then run
#     > docker image prune
# * Repeat the "Make a disk image" steps, above.
# * Test the new disk image. If it doesn't work, you can mark it "deprecated" in
#   the Image Family so new launches will use a previous Disk Image.
