#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Created on Mon Jan 04 2021 17:22:26 by codeskyblue
"""

import argparse
import base64
import datetime
import fnmatch
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from pprint import pformat, pprint
from typing import Optional, Union

import colored
import requests
from logzero import setup_logger

from ._wdaproxy import WDAService
from ._device import Device
from ._ipautil import IPAReader
from ._proto import MODELS, PROGRAM_NAME
from ._relay import relay
from ._usbmux import Usbmux
from ._utils import get_app_dir, get_binary_by_name, is_atty
from ._version import __version__
from .exceptions import MuxError, MuxServiceError, ServiceError

um = None  # Usbmux
logger = logging.getLogger(PROGRAM_NAME)


def _complete_udid(udid: Optional[str] = None) -> str:
    infos = um.device_list()
    if not udid:
        if len(infos) >= 2:
            sys.exit("More then 2 devices detected")
        if len(infos) == 0:
            sys.exit("No device detected")
        return infos[0]['SerialNumber']

    # Find udid exactly match
    for info in infos:
        if info['SerialNumber'] == udid:
            return udid

    # Find udid starts-with
    _udids = [
        info['SerialNumber'] for info in infos
        if info['SerialNumber'].startswith(udid)
    ]

    if len(_udids) == 1:
        return _udids[0]

    raise RuntimeError("No matched device", udid)


def _udid2device(udid: Optional[str] = None) -> Device:
    _udid = _complete_udid(udid)
    if _udid != udid:
        logger.debug("AutoComplete udid %s", _udid)
    del (udid)
    return Device(_udid, um)


def cmd_list(args: argparse.Namespace):
    if not args.json and is_atty:
        print("List of apple devices attached", file=sys.stderr)

    result = []
    for dinfo in um.device_list():
        udid = dinfo['SerialNumber']
        _d = Device(udid, um)
        name = _d.name
        if not args.json:
            print(udid, name)
        result.append(dict(udid=udid, name=name))
    if args.json:
        print(json.dumps(result, indent=4, ensure_ascii=False))


def cmd_device_info(args: argparse.Namespace):
    d = _udid2device(args.udid)
    value = d.get_value(no_session=args.simple, key=args.key, domain=args.domain)
    if args.json:
        def _bytes_hook(obj):
            if isinstance(obj, bytes):
                return base64.b64encode(obj).decode()
        print(json.dumps(value, indent=4, default=_bytes_hook))
    elif args.key or args.domain:
        pprint(value)
    else:
        print("{:17s} {}".format("MarketName:",
                                 MODELS.get(value['ProductType'])))
        for attr in ('DeviceName', 'ProductVersion', 'ProductType',
                     'ModelNumber', 'SerialNumber', 'PhoneNumber',
                     'CPUArchitecture', 'ProductName', 'ProtocolVersion',
                     'RegionInfo', 'TimeIntervalSince1970', 'TimeZone',
                     'UniqueDeviceID', 'WiFiAddress', 'BluetoothAddress',
                     'BasebandVersion'):
            print("{:17s} {}".format(attr + ":", value.get(attr)))


def cmd_version(args: argparse.Namespace):
    print(PROGRAM_NAME, "version", __version__)


def cmd_install(args: argparse.Namespace):
    d = _udid2device(args.udid)
    bundle_id = d.app_install(args.filepath_or_url)

    if args.launch:
        pid = d.instruments.app_launch(bundle_id)
        logger.info("Launch %r, process pid: %d", bundle_id, pid)


def cmd_uninstall(args: argparse.Namespace):
    d = _udid2device(args.udid)
    ok = d.app_uninstall(args.bundle_id)
    if not ok:
        sys.exit(1)


def cmd_reboot(args: argparse.Namespace):
    d = _udid2device(args.udid)
    print(d.reboot())


def cmd_parse(args: argparse.Namespace):
    uri = args.uri

    fp = None
    if re.match(r"^https?://", uri):
        try:
            import httpio
        except ImportError:
            print("Install missing lib: httpip")
            retcode = subprocess.call(
                [sys.executable, '-m', 'pip', 'install', '-U', 'httpio'])
            assert retcode == 0
            import httpio

        fp = httpio.open(uri, block_size=-1)
    else:
        assert os.path.isfile(uri)
        fp = open(uri, 'rb')

    try:
        ir = IPAReader(fp)
        ir.dump_info()
    finally:
        fp.close()


def cmd_watch(args: argparse.Namespace):
    """
    Info example:
    {'DeviceID': 13,
     'MessageType': 'Attached',
     'Properties': {'ConnectionSpeed': 480000000,
                    'ConnectionType': 'USB',
                    'DeviceID': 13,
                    'LocationID': 340918272,
                    'ProductID': 4776,
                    'SerialNumber': '84ad172e22d8372eb752f413280722cdcc200954',
                    'USBSerialNumber': '84ad172e22d8372eb752f413280722cdcc200954'}}
    """
    for info in um.watch_device():
        logger.info("%s", pformat(info))


def cmd_wait_for_device(args):
    u = Usbmux(args.socket)

    for info in u.watch_device():
        logger.debug("%s", pformat(info))
        if info['MessageType'] != 'Attached':
            continue
        udid = info['Properties']['SerialNumber']
        if args.udid is None:
            break
        if udid == args.udid:
            print("Device {!r} attached".format(
                info['Properties']['SerialNumber']))
            break


def cmd_xctest(args: argparse.Namespace):
    """
    Run XCTest required WDA installed.
    """
    d = _udid2device(args.udid)
    env = {}
    for kv in args.env or {}:
        key, val = kv.split(":", 1)
        env[key] = val
    if env:
        logger.info("Launch env: %s", env)
    d.xctest(args.bundle_id,
             target_bundle_id=args.target_bundle_id,
             logger=setup_logger(level=logging.INFO),
             env=env)


def cmd_screenshot(args: argparse.Namespace):
    d = _udid2device(args.udid)
    filename = args.filename or "screenshot.jpg"
    print("Screenshot saved to", filename)
    d.screenshot().convert("RGB").save(filename)


def cmd_appinfo(args: argparse.Namespace):
    d = _udid2device(args.udid)
    info = d.installation.lookup(args.bundle_id)
    if info is None:
        sys.exit(1)
    pprint(info)


def cmd_applist(args: argparse.Namespace):
    d = _udid2device(args.udid)
    # appinfos = d.installation.list_installed()
    # apps = d.instruments.app_list()
    # pprint(apps)
    for info in d.installation.iter_installed():
        # bundle_path = info['BundlePath']
        bundle_id = info['CFBundleIdentifier']

        try:
            display_name = info['CFBundleDisplayName']
            # major.minor.patch
            version = info.get('CFBundleShortVersionString', '')
            print(bundle_id, display_name, version)
        except BrokenPipeError:
            break
        # print(" ".join(
        #     (bundle_path, bundle_id, info['DisplayName'],
        #         info.get('Version', ''), info['Type'])))


def cmd_launch(args: argparse.Namespace):
    d = _udid2device(args.udid)
    try:
        pid = d.instruments.app_launch(args.bundle_id,
                                       args=args.arguments,
                                       kill_running=args.kill)
        print("PID:", pid)
    except ServiceError as e:
        sys.exit(e)


def cmd_kill(args: argparse.Namespace):
    d = _udid2device(args.udid)
    if args.name.isdigit():
        pid = int(args.name)
        d.app_kill(int(args.name))
    else:
        pid = d.app_kill(args.name)
        if pid is None:
            print("No app killed")
        else:
            print("Kill pid:", pid)


def cmd_system_info(args):
    d = _udid2device(args.udid)
    sinfo = d.instruments.system_info()
    pprint(sinfo)


def cmd_developer(args: argparse.Namespace):
    d = _udid2device(args.udid)
    d.mount_developer_image()
    return


def cmd_relay(args: argparse.Namespace):
    d = _udid2device(args.udid)
    relay(d, args.lport, args.rport, debug=args.x)


def cmd_wdaproxy(args: argparse.Namespace):
    """ start xctest and relay """
    d = _udid2device(args.udid)

    serv = WDAService(d, args.bundle_id)
    p = None
    if args.port:
        cmds = [
            sys.executable, '-m', 'tidevice', '-u', d.udid, 'relay',
            str(args.port), '8100'
        ]
        p = subprocess.Popen(cmds, stdout=sys.stdout, stderr=sys.stderr)

    try:
        serv.start()
        while serv._service.running:
            time.sleep(.1)
    finally:
        p and p.terminate()
        serv.stop()


def cmd_syslog(args: argparse.Namespace):
    d = _udid2device(args.udid)
    s = d.start_service("com.apple.syslog_relay")
    # print("SS")
    try:
        while True:
            text = s.recv().decode('utf-8')
            print(text, end='', flush=True)
    except (BrokenPipeError, IOError):
        # Python flushes standard streams on exit; redirect remaining output
        # to devnull to avoid another BrokenPipeError at shutdown
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())


def cmd_test(args: argparse.Namespace):

    print("Just test")
    # files = os.listdir(path)

    # Here need device unlocked
    # signatures = d.imagemounter.lookup()
    # if signatures:
    #     logger.info("DeveloperImage already mounted")
    #     return


_commands = [
    dict(action=cmd_version, command="version", help="show current version"),
    dict(action=cmd_list,
         command="list",
         flags=[
             dict(args=['--json'],
                  action='store_true',
                  help='output in json format')
         ],
         help="show connected iOS devices"),
    dict(action=cmd_device_info,
         command="info",
         flags=[
             dict(args=['--json'],
                  action='store_true',
                  help="output as json format"),
            dict(args=['-s', '--simple'], action='store_true', help='use a simple connection to avoid auto-pairing with the device'),
            dict(args=['-k', '--key'], type=str, help='only query specified KEY'),
            dict(args=['--domain'], help='set domain of query to NAME.'),
         ],
         help="show device info"),
    dict(action=cmd_system_info,
         command="sysinfo",
         help="show device system info"),
    dict(action=cmd_install,
         command="install",
         flags=[
             dict(args=['-L', '--launch'],
                  action='store_true',
                  help='launch after installed'),
             dict(args=['filepath_or_url'], help="local filepath or url")
         ],
         help="install application"),
    dict(action=cmd_uninstall,
         command="uninstall",
         flags=[dict(args=['bundle_id'], help="bundle_id of application")],
         help="uninstall application"),
    dict(action=cmd_reboot, command="reboot", help="reboot device"),
    dict(action=cmd_parse,
         command="parse",
         flags=[dict(args=['uri'], help="local path or url")],
         help="parse ipa bundle id"),
    dict(action=cmd_watch, command="watch", help="watch device"),
    dict(action=cmd_wait_for_device,
         command='wait-for-device',
         help='wait for device attached'),
    dict(action=cmd_xctest,
         command="xctest",
         flags=[
             dict(args=['-B', '--bundle_id', '--bundle-id'],
                  default="com.facebook.*.xctrunner",
                  help="bundle id of the test to launch"),
             dict(args=['--target-bundle-id'],
                  help='bundle id of the target app [optional]'),
             dict(args=['-I', '--install-wda'],
                  action='store_true',
                  help='install webdriveragent app'),
             dict(args=['-e', '--env'],
                  action='append',
                  help="set env with format key:value, support multi -e"),
         ],
         help="run XCTest"),
    dict(action=cmd_screenshot,
         command="screenshot",
         help="take screenshot",
         flags=[dict(args=['filename'], nargs="?", help="output filename")]),
    dict(action=cmd_applist, command="applist", help="list packages"),
    dict(action=cmd_launch,
         command="launch",
         flags=[
             dict(args=['--kill'],
                  action='store_true',
                  help='kill app if running'),
             dict(args=["bundle_id"], help="app bundleId"),
             dict(args=['arguments'], nargs='*', help='app arguments'),
         ],
         help="launch app with bundle_id"),
    dict(action=cmd_developer,
         command="developer",
         help="mount developer image to device"),
    dict(action=cmd_kill,
         command="kill",
         flags=[dict(args=['name'], help='pid or bundle_id')],
         help="kill by pid or bundle_id"),
    dict(action=cmd_relay,
         command="relay",
         flags=[
             dict(args=['-x'],
                  action='store_true',
                  help='verbose data traffic, hexadecimal'),
             dict(args=['lport'], type=int, help='local port'),
             dict(args=['rport'], type=int, help='remote port'),
         ],
         help="relay phone inner port to pc, same as iproxy"),
    dict(action=cmd_wdaproxy,
         command='wdaproxy',
         flags=[
             dict(args=['-B', '--bundle_id'],
                  default="com.facebook.*.xctrunner",
                  help="test application bundle id"),
             dict(args=['-p', '--port'],
                  type=int,
                  default=8100,
                  help='pc listen port, set to 0 to disable port forward')
         ],
         help='keep WDA running and relay WDA service to pc'),
    dict(action=cmd_syslog, command='syslog', help="print iphone syslog"),
    dict(action=cmd_test, command="test", help="command for developer"),
]


def main():
    # yapf: disable
    parser = argparse.ArgumentParser(
        description="Tool for communicate with iOS devices, version {}, created: codeskyblue 2020/05".format(__version__),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-v", "--version", action="store_true", help="show current version"),
    parser.add_argument("-u", "--udid", help="specify unique device identifier")
    parser.add_argument("--socket", help="usbmuxd listen address, host:port or local-path")

    subparser = parser.add_subparsers(dest='subparser')
    actions = {}
    for c in _commands:
        cmd_name = c['command']
        actions[cmd_name] = c['action']
        sp = subparser.add_parser(cmd_name, help=c.get('help'),
                                  formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        for f in c.get('flags', []):
            args = f.get('args')
            if not args:
                args = ['-'*min(2, len(n)) + n for n in f['name']]
            kwargs = f.copy()
            kwargs.pop('name', None)
            kwargs.pop('args', None)
            sp.add_argument(*args, **kwargs)

    args = parser.parse_args()

    if args.version:
        print(__version__)
        return

    if not args.subparser:
        parser.print_help()
        # show_upgrade_message()
        return

    global um
    um = Usbmux(args.socket)
    actions[args.subparser](args)
    # yapf: enable


if __name__ == "__main__":
    main()
