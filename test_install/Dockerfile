FROM debian:bookworm
ENV container=docker

RUN apt-get update && \
  apt-get install -y --no-install-recommends \
      systemd systemd-sysv sudo curl jq git python3 python3-venv && \
  rm -rf /var/lib/apt/lists/*

# Remove units that don't make sense in a container
RUN rm -f /lib/systemd/system/multi-user.target.wants/* \
        /etc/systemd/system/*.wants/* \
        /lib/systemd/system/local-fs.target.wants/* \
        /lib/systemd/system/sockets.target.wants/*udev* \
        /lib/systemd/system/sockets.target.wants/*initctl* \
        /lib/systemd/system/basic.target.wants/*

RUN useradd -m -s /bin/bash testbirder && \
  echo "testbirder ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/testbirder

CMD ["/lib/systemd/systemd"]
