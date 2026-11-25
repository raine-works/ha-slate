# Only the DRM backend and the kiosk shell are needed on the panel.
PACKAGECONFIG:remove = "shell-ivi shell-fullscreen wayland x11 xwayland rdp vnc remoting screenshare"
PACKAGECONFIG:append = " systemd"
