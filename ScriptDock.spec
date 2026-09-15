# Build: python -m PyInstaller --clean --noconfirm ScriptDock.spec
from pathlib import Path

root = Path(SPECPATH)
a = Analysis([str(root / 'main.py')], pathex=[str(root)],
             binaries=[], datas=[(str(root / 'assets' / 'tray.svg'), 'assets')],
             hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=[], noarchive=False)
# Qt uses the Windows 10/11 built-in ICU API (unversioned symbols).
# A development Python distribution may ship its own incompatible ICU and
# legacy API-set forwarders. Do not let those shadow the OS libraries.
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in {'icuuc.dll', 'icudt78.dll', 'ucrtbase.dll'}
              and not Path(entry[0]).name.lower().startswith('api-ms-win-')]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='ScriptDock',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=str(root / 'assets' / 'tray.ico'))
