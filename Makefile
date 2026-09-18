# Developer and deployment entry points. Everything runs from the project
# virtualenv created by `make install` (scripts/install.sh).

VENV ?= .venv
PY   := $(VENV)/bin/python

SYSTEMD_DIR := /etc/systemd/system
SUDOERS_DIR := /etc/sudoers.d
UNITS       := deploy/systemd/lattice-cam.service deploy/systemd/mediamtx-srt.service
SUDOERS     := deploy/sudoers.d/lattice-cam

.PHONY: install test lint typecheck check install-units render-units

install:
	./scripts/install.sh

test:
	$(PY) -m pytest -q

lint:
	$(VENV)/bin/ruff check . && $(VENV)/bin/ruff format --check .

typecheck:
	$(VENV)/bin/mypy

check: lint typecheck test

# The files under deploy/ are templates. Two placeholders are rendered here:
#   @INSTALL_DIR@   the checkout path         (default: this directory)
#   @SERVICE_USER@  the unprivileged account  (default: the invoking user)
# Override on the command line, e.g.
#   make install-units SERVICE_USER=rpi-cam INSTALL_DIR=/opt/lattice-cam
INSTALL_DIR  ?= $(CURDIR)
# Under `sudo make install-units` the invoking user is SUDO_USER, not root.
SERVICE_USER ?= $(if $(SUDO_USER),$(SUDO_USER),$(shell id -un))
RENDER_DIR   := build
RENDERED_UNITS   := $(addprefix $(RENDER_DIR)/,$(UNITS))
RENDERED_SUDOERS := $(RENDER_DIR)/$(SUDOERS)

# Renders one template into build/deploy/ with the placeholders substituted.
$(RENDER_DIR)/%: %
	@mkdir -p $(dir $@)
	sed -e 's|@INSTALL_DIR@|$(INSTALL_DIR)|g' -e 's|@SERVICE_USER@|$(SERVICE_USER)|g' $< > $@

.PHONY: render-units
render-units: $(RENDERED_UNITS) $(RENDERED_SUDOERS)
	@echo "Rendered into $(RENDER_DIR)/ with INSTALL_DIR=$(INSTALL_DIR) SERVICE_USER=$(SERVICE_USER)"

# Renders the templates, validates the sudoers rule, installs the two units
# and the rule (needs sudo) and reloads systemd. Does not enable or start
# anything: `sudo systemctl enable --now lattice-cam` afterwards.
# mediamtx-srt must NOT be enabled; the daemon starts it on a Start task.
install-units: render-units
	@echo "Validating $(RENDERED_SUDOERS)"
	visudo -cf $(RENDERED_SUDOERS)
	@echo "Installing units to $(SYSTEMD_DIR): $(RENDERED_UNITS)"
	sudo install -m 0644 $(RENDERED_UNITS) $(SYSTEMD_DIR)/
	@echo "Installing sudoers rule to $(SUDOERS_DIR)/lattice-cam (mode 0440)"
	sudo install -m 0440 -o root -g root $(RENDERED_SUDOERS) $(SUDOERS_DIR)/lattice-cam
	@echo "Reloading systemd"
	sudo systemctl daemon-reload
	@echo "Done. If mediamtx-srt was enabled by an older install: sudo systemctl disable --now mediamtx-srt"
	@echo "Then: sudo systemctl enable --now lattice-cam"
