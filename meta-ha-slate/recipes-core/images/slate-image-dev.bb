require slate-image.bb

SUMMARY = "HA Slate development image: production image + SSH, debug tools, root login"

IMAGE_FEATURES += "ssh-server-dropbear tools-debug allow-empty-password empty-root-password allow-root-login post-install-logging"

CORE_IMAGE_EXTRA_INSTALL += " \
    weston-examples \
    evtest \
    i2c-tools \
    alsa-utils \
    pipewire-tools \
    htop \
    strace \
    libdrm-tests \
"
