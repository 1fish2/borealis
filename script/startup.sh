#!/usr/bin/env bash

cd "$HOME/borealis" || { echo "Failure"; exit 1; }
git pull origin
pip install --upgrade borealis-fireworker

# init pyenv
[[ -f "$HOME/.bash_aliases" ]] && source "$HOME/.bash_aliases"

python -m borealis.fireworker
