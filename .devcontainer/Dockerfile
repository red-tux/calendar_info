# Dev container for the Calendar Info StreamController plugin.
#
# Derived from StreamController's own .devcontainer/Dockerfile
# (https://github.com/StreamController/StreamController/tree/main/.devcontainer), with
# these differences:
#   - StreamController itself is cloned into /workspaces/StreamController at image build time (branch, tag or
#     commit selected with STREAMCONTROLLER_REF), because the workspace of this repo is
#     the plugin, not the app.
#   - Pillow's build dependencies are installed so post-create.sh can rebuild Pillow
#     against the system FreeType (see that script for why).
#   - sudo is available to the container user so the start script can fix volume
#     ownership.
#   - Directories that get mounted as named volumes are pre-created with the right
#     owner, so a fresh volume inherits it.
FROM ubuntu:25.04

ARG PYTHON_VERSION=3.12
ARG STREAMCONTROLLER_REPO=https://github.com/StreamController/StreamController.git
ARG STREAMCONTROLLER_REF=main

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl git sudo ca-certificates build-essential cmake pkg-config \
    libgtk-4-dev libadwaita-1-dev \
    libdbus-1-dev dbus dbus-x11 \
    python3-gi python3-gi-cairo python3-dev python3-dbus \
    gobject-introspection libgirepository-2.0-dev \
    libgirepository1.0-dev \
    gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-glib-2.0 \
    libportal-dev gir1.2-xdp-1.0 libhidapi-libusb0 \
    libfreetype-dev libjpeg-dev zlib1g-dev libpng-dev libtiff-dev libwebp-dev \
    libopenjp2-7-dev liblcms2-dev libharfbuzz-dev libfribidi-dev \
    xdg-utils \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# The container user is the image's stock `ubuntu` account (uid 1000).
RUN echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu && chmod 0440 /etc/sudoers.d/ubuntu

# dbus socket location; the session bus itself is spawned by post-start.sh.
RUN mkdir -p /run/user/1000 && chown 1000:1000 /run/user/1000 && chmod 700 /run/user/1000

# Pre-create every directory that devcontainer.json mounts a named volume on, owned by
# the container user. Docker copies an image directory's contents and ownership into a
# brand-new named volume on first mount, so this is what keeps those volumes writable.
RUN mkdir -p /app /workspaces/StreamController/data/plugins /home/ubuntu/.claude /commandhistory \
    && chown -R ubuntu:ubuntu /app /workspaces /home/ubuntu/.claude /commandhistory \
    && touch /commandhistory/.bash_history && chown ubuntu:ubuntu /commandhistory/.bash_history

COPY --chown=ubuntu:ubuntu scripts/fetch-streamcontroller.sh /usr/local/bin/fetch-streamcontroller
RUN chmod +x /usr/local/bin/fetch-streamcontroller

USER ubuntu
WORKDIR /workspaces/StreamController

ENV PATH="/app/.venv/bin:/home/ubuntu/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HISTFILE=/commandhistory/.bash_history

# StreamController source at the requested ref (branch, tag or full commit sha).
RUN fetch-streamcontroller "${STREAMCONTROLLER_REPO}" "${STREAMCONTROLLER_REF}" /workspaces/StreamController

# uv, then a Python of the requested version and the app's virtualenv at /app/.venv
# (upstream keeps the venv at /app/.venv; the source lives at /workspaces/StreamController, which is where VS Code puts it when the app itself is the workspace).
RUN curl -LsSf https://astral.sh/uv/install.sh | bash
RUN uv python install ${PYTHON_VERSION} && \
    uv venv -p ${PYTHON_VERSION} /app/.venv && \
    echo 'export PATH="/app/.venv/bin:$PATH"' >> ~/.bashrc && \
    echo 'export PROMPT_COMMAND="history -a"' >> ~/.bashrc

CMD ["/bin/bash"]
