#!/usr/bin/env python3
"""Apply the MT6781 bring-up changes to an iOS root made by tools/mkiosroot.sh.

  sudo root-tweaks.py MOUNTED_ROOT [DEVICE]

MOUNTED_ROOT is the HFS+ image mounted read-write (Linux: run
`fsck.hfsplus -fy` on the image, then `mount -t hfsplus -o loop,force`).
Run as root (it writes root/mobile owned files). Idempotent. This is what
turned the plain mkiosroot.sh output into the images that reach the home
screen (image versions v8-v14 in NOTES.md):

  launchd.plist   _PanicOnCrash -> _PanicOnCrasX (a crashing daemon must not
                  panic the system); CommCenter jobs removed (no baseband: it
                  crash-loops and Setup blocks on its CoreTelephony XPC); bluetoothd
                  too, unless DEVICE's device.conf has `bluetooth 1`; mediaserverd's
                  capture services (torch, capture source and session, viewfinder,
                  deferred container) to mapple-captured if the root has it, with
                  a 200 MB jetsam limit (jetsamproperties: its preview buffers and
                  photos), and AVFoundation logging the API violations it would
                  throw for (Camera.app asks the iPhone X's photo features; the
                  reason shows in the log), Camera's Formats set to Most Compatible;
                  lskdd and wifip2pd (relaunched about once a second)
  containers      /private/var/containers/{Bundle,Data/System,Shared/SystemGroup,Temp}
                  (on iOS they live on the data volume)
  migration       DataMigration runs once at the first boot with only
                  SystemAppMigrator (the 32 apps staged in /private/var
                  installed; the other migrators in DataClassMigrators.disabled:
                  all of them take an hour and broke SpringBoard); no
                  LastSystemVersion, which datamigrator then records itself
  audio           VirtualAudio HAL plug-in disabled (mediaserverd crash); with
                  `audio 1` in DEVICE's device.conf it stays, and the audio
                  plug-in (mapple/userland audio/build.sh) goes beside it
  MediaToolbox    StartRecorderServer=false (camera filter prewarm calls a
                  NULL OpenGL function without a GPU -> mediaserverd crash loop)
  SpringBoard     BKNoWatchdogs (one slow CPU path, no scene-create watchdog
                  kills), no cloud-configuration UI
  Setup           skipped: SetupDone, SetupFinishedAllSteps, SetupVersion 11,
                  mini-buddy flags off
  activation      mobileactivationd data ark seeded; the actual Activated state
                  comes from its Hactivation branch, selected by the /chosen
                  security-status properties in tools/mkadt.py
  light UI        with DEVICE and `light_ui 1` in its device.conf: Reduce Motion,
                  Reduce Transparency and Prefer Cross-Fade Transitions on for
                  the mobile user
"""
import glob, os, plistlib, shutil, sys

BUILD, VERSION = '20H392', '16.7.16'
MOBILE = 501


def load(path):
    with open(path, 'rb') as f:
        return plistlib.load(f)


def save(path, data, uid, gid, mode, fmt=plistlib.FMT_BINARY):
    with open(path, 'wb') as f:
        plistlib.dump(data, f, fmt=fmt)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def merge_pref(root, name, values):
    """Merge keys into /var/mobile/Library/Preferences/NAME.plist (mobile, 0600)."""
    path = os.path.join(root, 'private/var/mobile/Library/Preferences', name + '.plist')
    data = load(path) if os.path.exists(path) else {}
    data.update(values)
    save(path, data, MOBILE, MOBILE, 0o600)
    print('pref', name, sorted(values))


CAPTURED = '/usr/libexec/mapple-captured'
VIEWFINDER = 'com.apple.coremedia.cameraviewfinder'    # NSXPC, not ours to answer: left with nobody
CAPTURE_SERVICES = ('com.apple.coremedia.flashlight', 'com.apple.coremedia.capturesource',
                    'com.apple.coremedia.capturesession', VIEWFINDER,
                    'com.apple.coremedia.capture.deferred.containermanager.xpc')


def launchd(root, bluetooth):
    path = os.path.join(root, 'System/Library/xpc/launchd.plist')
    st = os.stat(path)
    plist = load(path)
    jobs = plist['LaunchDaemons']
    renamed = 0
    for job in jobs.values():
        if '_PanicOnCrash' in job:
            job['_PanicOnCrasX'] = job.pop('_PanicOnCrash')
            renamed += 1
    # no baseband; bluetoothd and fairplayd.H2 restart in a loop without Bluetooth and media support
    # lskdd: its daily activity never completes, launchd starts it again at once; wifip2pd: its sandbox check of the
    # caller fails, it exits and is asked again (no AWDL here): each about once a second
    labels = ('com.apple.CommCenter', 'com.apple.fairplayd.H2', 'com.apple.lskdd', 'com.apple.wifip2pd') + \
        (() if bluetooth else ('com.apple.bluetoothd',))
    removed = [k for k, j in jobs.items() if j.get('Label') in labels]
    for k in removed:
        del jobs[k]
    # The recorder server is off (MediaToolbox below), so mediaserverd never answers its capture services, and a client
    # (SpringBoard's torch and mic indicator) waits on them with no timeout. They go to our stand-in
    # (mapple/userland tools/mapple-captured: the torch, no cameras) when the root has it; without it a lookup fails.
    for job in jobs.values():
        if job.get('Label') == 'com.apple.mediaserverd':
            for name in CAPTURE_SERVICES:
                job.get('MachServices', {}).pop(name, None)
    standin = os.path.exists(os.path.join(root, CAPTURED.lstrip('/')))
    if standin:
        jobs['/System/Library/LaunchDaemons/org.openmapple.mapple-captured.plist'] = {
            'Label': 'org.openmapple.mapple-captured',
            'Program': CAPTURED,
            'ProgramArguments': [CAPTURED],
            'MachServices': {name: True for name in CAPTURE_SERVICES if name != VIEWFINDER},
            'UserName': 'root',
            'StandardErrorPath': '/dev/console',
        }
    save(path, plist, st.st_uid, st.st_gid, st.st_mode & 0o7777)
    print(f'launchd.plist: {renamed} _PanicOnCrash renamed, {len(removed)} jobs removed, capture services to '
          f'{"mapple-captured" if standin else "nobody"}')
    # launchd's per-model jetsam table; without an entry a daemon gets a few MB
    tables = glob.glob(os.path.join(root, 'System/Library/LaunchDaemons/com.apple.jetsamproperties.*.plist'))
    for path in tables if standin else ():
        st = os.stat(path)
        data = load(path)
        data['Version4']['Daemon']['Override']['org.openmapple.mapple-captured'] = {
            'JetsamPriority': 140, 'ActiveSoftMemoryLimit': 200, 'InactiveHardMemoryLimit': 200}
        save(path, data, st.st_uid, st.st_gid, st.st_mode & 0o7777)
        print('jetsam: mapple-captured 200 MB in', os.path.basename(path))
    if standin:
        merge_pref(root, 'com.apple.avfoundation', {'avcapture_pardon_api_violations': 1})
        # Camera's Formats: Most Compatible (JPEG); High Efficiency asks HEIC, which nothing here encodes
        merge_pref(root, 'com.apple.camera', {'CAMUserPreferenceCaptureEncodingBehavior': 1})


def containers(root):
    base = os.path.join(root, 'private/var/containers')
    for sub, uid in (('Bundle', 33), ('Data/System', 0), ('Shared/SystemGroup', 0), ('Temp', 0)):
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            os.makedirs(d)
            os.chown(d, uid, uid)
            os.chmod(d, 0o755)
            print('mkdir', d)


def find_container(root, kind, identifier):
    for meta in glob.glob(os.path.join(root, 'private/var/containers', kind, '*',
                                       '.com.apple.mobile_container_manager.metadata.plist')):
        if load(meta).get('MCMMetadataIdentifier') == identifier:
            return os.path.dirname(meta)
    return None


def activation(root):
    c = find_container(root, 'Data/System', 'com.apple.mobileactivationd')
    if c is None:
        print('warning: no mobileactivationd system container in the image; skipped data ark')
        return
    d = os.path.join(c, 'Library/internal')
    os.makedirs(d, exist_ok=True)
    os.chown(d, MOBILE, MOBILE)
    os.chmod(d, 0o755)
    save(os.path.join(d, 'data_ark.plist'), {
        '-ActivationState': 'Activated', '-BrickState': False,
        '-BuildVersion': BUILD, '-ProductVersion': VERSION,
        '-DataMigrationCompleted': True, '-PreSydroDataMigrationCompleted': True,
    }, MOBILE, MOBILE, 0o600)
    print('data ark', d)


def audio(root, plugin):
    src = os.path.join(root, 'Library/Audio/Plug-Ins/HAL/VirtualAudio.plugin')
    dst_dir = os.path.join(root, 'Library/Audio/Plug-Ins/HAL.disabled')
    if plugin:
        # VirtualAudio routes iOS's sound to MappleAudio's "Speaker"; put back if an earlier run moved it
        if os.path.exists(os.path.join(dst_dir, 'VirtualAudio.plugin')) and not os.path.exists(src):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(os.path.join(dst_dir, 'VirtualAudio.plugin'), src)
            print('VirtualAudio.plugin enabled again')
        dst = os.path.join(root, 'System/Library/Audio/Plug-Ins/HAL/MappleAudio.driver')
        if not os.path.isdir(dst):   # mka puts it on the root first
            top = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../..')
            bundle = os.path.join(top, 'mapple/userland/audio/out/MappleAudio.driver')
            if not os.path.isdir(bundle):
                sys.exit(bundle + ' missing: mapple/userland/audio/build.sh first')
            shutil.copytree(bundle, dst)
        for d, _, files in os.walk(dst):
            os.chown(d, 0, 0)
            os.chmod(d, 0o755)
            for f in files:
                os.chown(os.path.join(d, f), 0, 0)
                os.chmod(os.path.join(d, f), 0o755 if f == 'MappleAudio' else 0o644)
        print('MappleAudio.driver installed')
    elif os.path.exists(src):
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, dst_dir)
        print('VirtualAudio.plugin disabled')
    for conf in glob.glob(os.path.join(root, 'System/Library/Frameworks/MediaToolbox.framework/*/SubServerConfiguration.plist')):
        st = os.stat(conf)
        data = load(conf)
        data['StartRecorderServer'] = False
        save(conf, data, st.st_uid, st.st_gid, st.st_mode & 0o7777, plistlib.FMT_XML)
        print('StartRecorderServer off', os.path.relpath(conf, root))


def migration(root):
    merge_pref(root, 'com.apple.migration', {
        'DMUserDataDisposition': {'SyntheticDidUpgrade': False, 'buildVersion': BUILD}})
    path = os.path.join(root, 'private/var/mobile/Library/Preferences/com.apple.migration.plist')
    data = load(path)
    if data.pop('LastSystemVersion', None) is not None:
        save(path, data, MOBILE, MOBILE, 0o600)
    src = os.path.join(root, 'System/Library/DataClassMigrators')
    dst = src + '.disabled'
    moved = [m for m in sorted(os.listdir(src)) if m != 'SystemAppMigrator.migrator']
    if moved:
        if not os.path.isdir(dst):
            st = os.stat(src)
            os.mkdir(dst)
            os.chown(dst, st.st_uid, st.st_gid)
            os.chmod(dst, st.st_mode & 0o7777)
        for m in moved:
            shutil.move(os.path.join(src, m), os.path.join(dst, m))
    print(f'migration: SystemAppMigrator alone, {len(moved)} migrators to DataClassMigrators.disabled')


def device_conf(name):
    """The settings of `device NAME` in device/*/NAME/device.conf of this checkout."""
    top = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../..')
    paths = glob.glob(os.path.join(top, 'device/*', name, 'device.conf'))
    if len(paths) != 1:
        sys.exit('no device.conf for ' + name)
    conf, inside = {}, False
    for line in open(paths[0]):
        if line.startswith('device '):
            inside = line.split()[1] == name
        elif inside and line.startswith('\t'):
            key, _, value = line.strip().partition('\t')
            conf[key] = value
    return conf


def light_ui(root):
    # the keys libAccessibility's setters write (_AXSSetReduceMotionEnabled,
    # _AXSSetEnhanceBackgroundContrastEnabled, _AXSSetReduceMotionReduceSlideTransitionsEnabled);
    # AXSDidResetD22Preferences: AccessibilitySettingsLoader's one-time _resetD22ReduceMotion is done already
    merge_pref(root, 'com.apple.Accessibility', {
        'ReduceMotionEnabled': True,
        'EnhancedBackgroundContrastEnabled': True,
        'ReduceMotionReduceSlideTransitionsPreference': True,
        'AXSDidResetD22Preferences': True})


def main():
    if len(sys.argv) not in (2, 3) or not os.path.isdir(os.path.join(sys.argv[1], 'System/Library/xpc')):
        sys.exit(__doc__)
    root = sys.argv[1]
    conf = device_conf(sys.argv[2]) if len(sys.argv) == 3 else {}
    launchd(root, conf.get('bluetooth') == '1')
    containers(root)
    activation(root)
    audio(root, conf.get('audio') == '1')
    migration(root)
    merge_pref(root, 'com.apple.springboard', {
        'BKNoWatchdogs': True,
        'FBLaunchWatchdogExceptions': {'com.apple.purplebuddy': 100.0},
        'MCForceShowCloudConfigurationUI': False})
    merge_pref(root, 'com.apple.purplebuddy', {
        'SetupDone': True, 'SetupFinishedAllSteps': True, 'SetupVersion': 11,
        'daemonizeNeedsToRun': False})
    merge_pref(root, 'com.apple.purplebuddy.notbackedup', {
        k: False for k in ('ShouldRunDiagnosticsMiniBuddy', 'ShouldRunHSA2MiniBuddy',
                           'ShouldRunKeychainSyncMiniBuddy', 'ShouldRunMesaMiniBuddy',
                           'ShouldRunPaymentMiniBuddy', 'ShouldRunStandardMiniBuddy',
                           'SilentICDPUpgrade')})
    merge_pref(root, 'com.apple.managedconfiguration.notbackedup', {
        'LockdownActivationIndicatesCloudConfigurationAvailable': False})
    if conf.get('light_ui') == '1':
        light_ui(root)


if __name__ == '__main__':
    main()
