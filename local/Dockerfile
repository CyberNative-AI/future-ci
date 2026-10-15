# Runner-like base for the local engine: a subset of what actions/runner-images installs on
# ubuntu-24.04 / ubuntu-26.04, plus libfaketime. Jobs run as user "runner" (uid 1000, so bind mounts
# owned by the host user are readable) with passwordless sudo.
ARG BASE=ubuntu:24.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git curl wget ca-certificates sudo tzdata faketime python3 python3-venv \
      libffi-dev libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev libyaml-dev \
      libgdbm-dev libncurses-dev uuid-dev libxml2-dev libxslt1-dev libpq-dev libcurl4-openssl-dev \
      xz-utils unzip zip pkg-config openssh-client openssh-server locales file jq \
      tk bc \
    && rm -rf /var/lib/apt/lists/* \
    && (userdel -r ubuntu 2>/dev/null || true) \
    && useradd -m -u 1000 -s /bin/bash runner && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner \
    && mkdir -p /opt/hostedtoolcache && chown runner /opt/hostedtoolcache
USER runner
WORKDIR /home/runner
