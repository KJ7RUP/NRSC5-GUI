# nrsc5_gui.spec  —  PyInstaller spec
# Run: pyinstaller nrsc5_gui.spec

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=['.'],
    binaries=[
        ('libnrsc5.dll',  '.'),   # HD Radio decoder
        ('librtlsdr.dll', '.'),   # RTL-SDR hardware (from MSYS2 mingw64/bin)
    ],
    datas=[
        ('ui',      'ui'),        # Web UI
        ('nrsc5.py', '.'),        # nrsc5 Python API (from repo support/)
    ],
    hiddenimports=[
        'flask_socketio',
        'engineio', 'socketio',
        'eventlet',
        'eventlet.hubs.epolls', 'eventlet.hubs.kqueue', 'eventlet.hubs.selects',
        'dns', 'dns.resolver',
        'pyaudio',
        'numpy', 'numpy.core', 'numpy.core._multiarray_umath',
        'scipy', 'scipy.signal', 'scipy.signal._signaltools',
        'rtlsdr',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='nrsc5-gui',
    debug=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no black console window
    icon=None,              # set to 'icon.ico' if you have one
)
