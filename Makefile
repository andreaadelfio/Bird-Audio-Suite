SHELL := /bin/bash

SERVICE_NAME := bird-audio-suite.service
SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user
SERVICE_FILE := $(SYSTEMD_USER_DIR)/$(SERVICE_NAME)
PROJECT_DIR := $(abspath .)
CLI_SCRIPT := $(PROJECT_DIR)/bird_audio_cli.py

VENV_DIR ?= $(HOME)/.venvs/bird-audio-suite
PYTHON ?= $(VENV_DIR)/bin/python
BACKEND ?= sounddevice
DEVICE_INDEX ?= -1

.PHONY: venv test install start stop restart log log-follow status

# Dedicated environment outside Dropbox (NumPy 2 + LiteRT, no TensorFlow).
venv:
	uv venv --python python3.12 "$(VENV_DIR)"
	VIRTUAL_ENV="$(VENV_DIR)" uv pip install --python "$(VENV_DIR)/bin/python" -r requirements.txt

test:
	"$(PYTHON)" -m pytest -q tests

install:
	mkdir -p "$(SYSTEMD_USER_DIR)"
	printf '%s\n' \
		'[Unit]' \
		'Description=Bird Audio Suite live listener' \
		'After=default.target' \
		'' \
		'[Service]' \
		'Type=simple' \
		'WorkingDirectory=$(PROJECT_DIR)' \
		'Environment=PYTHONUNBUFFERED=1' \
		'ExecStart="$(PYTHON)" "$(CLI_SCRIPT)" live --backend $(BACKEND) --device-index $(DEVICE_INDEX)' \
		'Restart=always' \
		'RestartSec=5' \
		'StandardOutput=journal' \
		'StandardError=journal' \
		'' \
		'[Install]' \
		'WantedBy=default.target' > "$(SERVICE_FILE)"
	systemctl --user daemon-reload
	systemctl --user enable "$(SERVICE_NAME)"

start:
	systemctl --user start "$(SERVICE_NAME)"

stop:
	systemctl --user stop "$(SERVICE_NAME)"

restart:
	systemctl --user restart "$(SERVICE_NAME)"

log:
	journalctl --user -u "$(SERVICE_NAME)" -o cat -e

log-follow:
	journalctl --user -u "$(SERVICE_NAME)" -f -o cat

status:
	systemctl --user status "$(SERVICE_NAME)"
