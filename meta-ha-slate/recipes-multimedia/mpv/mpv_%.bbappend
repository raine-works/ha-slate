# The voice service drives mpv through python-mpv (ctypes -> libmpv.so.2),
# purely as an audio player for TTS, chimes and HA media. Build the shared
# library, output through the Pulse protocol (PipeWire's pulse shim), and drop
# the video/scripting stack the panel never uses.
DEPENDS:append = " pulseaudio"
PACKAGECONFIG:remove = "lua wayland egl opengl x11"
EXTRA_OECONF:append = " --enable-libmpv-shared --enable-pulse"

PACKAGES =+ "libmpv"
FILES:libmpv = "${libdir}/libmpv${SOLIBS}"
