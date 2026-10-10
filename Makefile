SHELL := /bin/bash

SERVICE_NAME := bird-audio-suite.service
SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user
SERVICE_FILE := $(SYSTEMD_USER_DIR)/$(SERVICE_NAME)
HEALTH_NAME := bird-audio-suite-health
SYNC_NAME := bird-audio-suite-sync
PROJECT_DIR := $(abspath .)
CLI_SCRIPT := $(PROJECT_DIR)/bird_audio_cli.py

VENV_DIR ?= $(HOME)/.venvs/bird-audio-suite
PYTHON ?= $(VENV_DIR)/bin/python
BACKEND ?= sounddevice
DEVICE_INDEX ?= -1
PORT ?= 8004
HOST ?= 127.0.0.1

.PHONY: venv test git dev dev-stop dev_mob web-dev install install-health install-sync start stop restart log log-follow status

# Dedicated environment outside Dropbox (NumPy 2 + LiteRT, no TensorFlow).
venv:
	uv venv --python python3.12 "$(VENV_DIR)"
	VIRTUAL_ENV="$(VENV_DIR)" uv pip install --python "$(VENV_DIR)/bin/python" -r requirements.txt

test:
	"$(PYTHON)" -m pytest -q tests

# Like the other projects: commit everything and publish (GitHub Pages serves docs/)
git:
	git add --all
	git diff --cached --quiet || git commit -m "Updated website at $(shell date)"
	git push

# The review site (docs/) against the real Supabase, at http://$(HOST):$(PORT)/
dev:
	@echo "Avvio server su http://$(HOST):$(PORT)/index.html"
	python3 -m http.server "$(PORT)" --bind "$(HOST)" --directory "$(PROJECT_DIR)/docs" >/dev/null 2>&1 &
	@if command -v xdg-open >/dev/null 2>&1; then \
		xdg-open "http://$(HOST):$(PORT)/index.html" >/dev/null 2>&1; \
	else \
		echo "Apri manualmente: http://$(HOST):$(PORT)/index.html"; \
	fi

# Mobile preview, like MyFinance: the site in a "phone" at http://127.0.0.1:8005/mobile and on the
# real phone (same Wi-Fi) at the address it prints; both reload by themselves at every save. Ctrl+C stops it.
dev_mob:
	python3 dev/dev_server.py 8005 --rete

# `stop` is taken by the live service
dev-stop:
	@if pkill -f "[p]ython3 -m http.server $(PORT)" 2>/dev/null; then \
		echo "Server fermato sulla porta $(PORT)"; \
	else \
		echo "Nessun server trovato sulla porta $(PORT)"; \
	fi

# Review site on a local stand-in for Supabase (embedded Postgres + PostgREST) with a read-only copy
# of the real data: http://127.0.0.1:8010
web-dev:
	"$(PYTHON)" dev/local_stack.py --snapshot

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

# Every 5 minutes: desktop notification if the live service is down, stuck or deaf.
install-health:
	mkdir -p "$(SYSTEMD_USER_DIR)"
	printf '%s\n' \
		'[Unit]' \
		'Description=Bird Audio Suite health check' \
		'' \
		'[Service]' \
		'Type=oneshot' \
		'WorkingDirectory=$(PROJECT_DIR)' \
		'ExecStart="$(PYTHON)" -m bird_audio_suite.health' > "$(SYSTEMD_USER_DIR)/$(HEALTH_NAME).service"
	printf '%s\n' \
		'[Unit]' \
		'Description=Run the Bird Audio Suite health check every 5 minutes' \
		'' \
		'[Timer]' \
		'OnBootSec=10min' \
		'OnUnitActiveSec=5min' \
		'' \
		'[Install]' \
		'WantedBy=timers.target' > "$(SYSTEMD_USER_DIR)/$(HEALTH_NAME).timer"
	systemctl --user daemon-reload
	systemctl --user enable --now "$(HEALTH_NAME).timer"

# Every 15 minutes: push new detections to Supabase (catches up after being offline).
install-sync:
	mkdir -p "$(SYSTEMD_USER_DIR)"
	printf '%s\n' \
		'[Unit]' \
		'Description=Bird Audio Suite sync to Supabase' \
		'After=network-online.target' \
		'' \
		'[Service]' \
		'Type=oneshot' \
		'WorkingDirectory=$(PROJECT_DIR)' \
		'ExecStart="$(PYTHON)" "$(CLI_SCRIPT)" sync' > "$(SYSTEMD_USER_DIR)/$(SYNC_NAME).service"
	printf '%s\n' \
		'[Unit]' \
		'Description=Run the Bird Audio Suite sync every 15 minutes' \
		'' \
		'[Timer]' \
		'OnBootSec=15min' \
		'OnUnitActiveSec=15min' \
		'' \
		'[Install]' \
		'WantedBy=timers.target' > "$(SYSTEMD_USER_DIR)/$(SYNC_NAME).timer"
	systemctl --user daemon-reload
	systemctl --user enable --now "$(SYNC_NAME).timer"

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
