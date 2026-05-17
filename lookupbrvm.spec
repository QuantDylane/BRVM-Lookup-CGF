# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file pour LOOK UP BRVM
Usage: pyinstaller lookupbrvm.spec
"""

import os

block_cipher = None
BASE_DIR = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    ['run.py'],
    pathex=[BASE_DIR],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('data', 'data'),
        ('dashboard', 'dashboard'),
        ('lookupbrvm', 'lookupbrvm'),
        ('db.sqlite3', '.'),
        ('scraper_brvm.py', '.'),
        ('scraper_news_brvm.py', '.'),
    ],
    hiddenimports=[
        'django',
        'django.contrib.admin',
        'django.contrib.auth',
        'django.contrib.contenttypes',
        'django.contrib.sessions',
        'django.contrib.messages',
        'django.contrib.staticfiles',
        'django.template.backends.django',
        'django.template.loaders.filesystem',
        'django.template.loaders.app_directories',
        'dashboard',
        'dashboard.apps',
        'dashboard.models',
        'dashboard.views',
        'dashboard.urls',
        'dashboard.templatetags',
        'dashboard.templatetags.dashboard_filters',
        'dashboard.management',
        'dashboard.management.commands',
        'dashboard.management.commands.import_data',
        'lookupbrvm.settings',
        'lookupbrvm.urls',
        'lookupbrvm.wsgi',
        'numpy',
        'pandas',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'scipy',
        'PIL',
        'IPython',
        'notebook',
        'pytest',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LookUpBRVM',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='LookUpBRVM',
)
