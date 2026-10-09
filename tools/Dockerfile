ARG DEBIAN_VERSION=trixie
FROM debian:${DEBIAN_VERSION}-slim
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    build-essential debhelper dpkg-dev fakeroot python3 python3-paho-mqtt lm-sensors \
    smartmontools mosquitto mosquitto-clients lintian systemd systemd-sysv \
    ca-certificates git make && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
