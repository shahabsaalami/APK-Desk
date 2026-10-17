"""APK Desk: local Android release management with Explorer integration."""
import argparse
import concurrent.futures
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import zipfile

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    BaseWindow = TkinterDnD.Tk
except ImportError:
    BaseWindow, DND_FILES = tk.Tk, None

DATA = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'APKDesk'
CONFIG = DATA / 'settings.json'
DEFAULT = {'active': 'Default', 'profiles': {'Default': {
    'client': '', 'project': '', 'pattern': '{app}_{version}_{code}',
    'archive': str(Path.home() / 'Documents' / 'APK Desk Archive')}}}
ACTIONS = [('info', 'مشخصات APK / Inspect'), ('rename', 'تغییر نام / Rename'),
           ('signature', 'بررسی امضا / Verify signature'), ('check', 'بررسی انتشار / Release check'),
           ('archive', 'آرشیو نسخه / Archive'), ('bundle', 'بسته تحویل / Delivery ZIP'),
           ('compare', 'مقایسه با نسخه دیگر / Compare'), ('install', 'نصب روی گوشی / Install APK'),
           ('open', 'باز کردن در APK Desk')]


def read_settings():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding='utf-8'))
    return json.loads(json.dumps(DEFAULT))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def tool(name):
    found = shutil.which(name)
    if found:
        return Path(found)
    for env in ('ANDROID_HOME', 'ANDROID_SDK_ROOT'):
        root = os.environ.get(env)
        if root:
            if name == 'adb':
                candidate = Path(root) / 'platform-tools' / 'adb.exe'
                if candidate.is_file():
                    return candidate
            matches = sorted((Path(root) / 'build-tools').glob('*/' + name + '.exe'), reverse=True)
            if matches:
                return matches[0]
    package = 'Platform-Tools' if name == 'adb' else 'Build-Tools'
    raise RuntimeError(f'{name} پیدا نشد. Android SDK {package} را به PATH اضافه کنید.')


def run(args, timeout=120):
    result = subprocess.run([str(x) for x in args], capture_output=True, encoding='utf-8',
                            errors='replace', timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'Command failed').strip())
    return result.stdout


def connected_devices():
    output = run([tool('adb'), 'devices', '-l'])
    devices, blocked = [], []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or line.startswith(('List of devices', '*')):
            continue
        serial, state = parts[:2]
        if state == 'device':
            model = next((p[6:].replace('_', ' ') for p in parts[2:] if p.startswith('model:')), serial)
            devices.append({'serial': serial, 'label': f'{model} — {serial}'})
        else:
            blocked.append(f'{serial}: {state}')
    if not devices:
        details = '\n' + '\n'.join(blocked) if blocked else ''
        raise RuntimeError('دستگاه آماده‌ای پیدا نشد. گوشی را وصل کنید، USB debugging را فعال کنید '
                           'و پیام اجازهٔ اتصال روی گوشی را تأیید کنید. شبیه‌ساز و اتصال بی‌سیم ADB نیز پشتیبانی می‌شوند.' + details)
    return devices


def install_apk(path, serial):
    path = Path(path).resolve()
    if path.suffix.lower() != '.apk' or not path.is_file():
        raise ValueError('یک فایل APK معتبر انتخاب کنید.')
    if not serial:
        raise ValueError('دستگاه مقصد انتخاب نشده است.')
    output = run([tool('adb'), '-s', serial, 'install', '-r', path], timeout=300)
    if not re.search(r'^Success\s*$', output, re.MULTILINE):
        raise RuntimeError(output.strip() or 'ADB موفقیت نصب را تأیید نکرد.')
    return f'نصب روی {serial} انجام شد.\n' + output.strip()


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def inspect_apk(path):
    path = Path(path).resolve()
    if path.suffix.lower() != '.apk' or not path.is_file():
        raise ValueError('یک فایل APK معتبر انتخاب کنید.')
    output = run([tool('aapt'), 'dump', 'badging', path])
    def field(pattern, default=''):
        match = re.search(pattern, output, re.MULTILINE)
        return match.group(1) if match else default
    package = field(r"^package: name='([^']+)'")
    if not package:
        raise ValueError('اطلاعات پکیج از APK قابل استخراج نیست.')
    return {'path': str(path), 'app': field(r"^application-label:'([^']+)'", package),
            'package': package, 'version': field(r"versionName='([^']*)'", 'unknown'),
            'code': field(r"versionCode='([^']*)'"), 'min_sdk': field(r"^sdkVersion:'([^']*)'"),
            'target_sdk': field(r"^targetSdkVersion:'([^']*)'"),
            'architectures': field(r"^native-code: (.*)", 'No native libraries'),
            'debuggable': bool(re.search(r'^application-debuggable', output, re.MULTILINE)),
            'permissions': sorted(set(re.findall(r"^uses-permission[^:]*: name='([^']+)'", output, re.MULTILINE))),
            'bytes': path.stat().st_size, 'sha256': digest(path)}


def signature(path):
    jar = tool('aapt').parent / 'lib' / 'apksigner.jar'
    if not jar.exists():
        raise RuntimeError(f'apksigner.jar پیدا نشد: {jar}')
    java = shutil.which('java')
    if not java:
        raise RuntimeError('Java در PATH پیدا نشد.')
    output = run([java, '-jar', jar, 'verify', '--verbose', '--print-certs', path])
    return {'verified': True,
            'certificates': sorted(re.findall(r'^Signer #\d+ certificate SHA-256 digest: (.+)$', output, re.MULTILINE)),
            'debug_certificate': 'CN=Android Debug' in output, 'details': output}


def safe_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip().rstrip('. ')
    value = re.sub(r'\s+', '_', value)
    if not value:
        return 'Unknown'
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM'+str(i) for i in range(1,10)} | {'LPT'+str(i) for i in range(1,10)}
    if value.split('.')[0].upper() in reserved:
        value = '_' + value
    return value[:160]


def filename(info, profile):
    values = {**info, 'client': profile['client'], 'project': profile['project'],
              'date': datetime.now().strftime('%Y-%m-%d')}
    try:
        rendered = profile['pattern'].format_map({k: safe_component(v) for k,v in values.items()})
    except (KeyError, ValueError, AttributeError, IndexError) as error:
        raise ValueError('الگوی نام معتبر نیست. از app, version, code, package, date, client, project استفاده کنید.') from error
    return safe_component(rendered.removesuffix('.apk')) + '.apk'


def archive_entries(profile, package):
    root = Path(profile['archive']).expanduser() / safe_component(package)
    entries = []
    if root.exists():
        for report in root.glob('*/release.json'):
            try:
                entry = json.loads(report.read_text(encoding='utf-8'))
                if isinstance(entry, dict) and isinstance(entry.get('info'), dict):
                    entries.append(entry)
            except (ValueError, OSError):
                pass
    return sorted(entries, key=lambda x: x.get('created', ''), reverse=True)


def release_check(info, profile):
    warnings, sig = [], None
    try:
        sig = signature(info['path'])
    except Exception as error:
        warnings.append('بررسی امضا ناموفق: ' + str(error))
    if info['debuggable'] or (sig and sig['debug_certificate']):
        warnings.append('این خروجی debug است یا گواهی Android Debug دارد.')
    entries = archive_entries(profile, info['package'])
    if entries:
        previous = entries[0]
        old = previous['info']
        if any(x['info']['code'] == info['code'] for x in entries):
            warnings.append('versionCode قبلاً در آرشیو ثبت شده است.')
        if info['code'].isdigit() and old['code'].isdigit() and int(info['code']) < int(old['code']):
            warnings.append('versionCode از آخرین نسخهٔ آرشیو کمتر است.')
        if old['bytes'] and info['bytes'] > old['bytes'] * 1.2:
            warnings.append('حجم نسبت به آخرین نسخه بیش از ۲۰٪ افزایش یافته است.')
        old_sig = previous.get('signature')
        if sig and old_sig and sig['certificates'] != old_sig['certificates']:
            warnings.append('گواهی امضا متفاوت است؛ چرخش مجاز کلید باید جداگانه بررسی شود.')
    else:
        warnings.append('نسخهٔ قبلی برای مقایسه در آرشیو وجود ندارد.')
    return {'warnings': warnings, 'signature': sig,
            'scope': 'بررسی محلی مشخصات و امضا؛ جایگزین تست برنامه یا تأیید فروشگاه نیست.'}


def archive_apk(info, profile, notes=''):
    root = Path(profile['archive']).expanduser() / safe_component(info['package'])
    root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=safe_component(info['version']) + '_', dir=root))
    try:
        destination = folder / filename(info, profile)
        shutil.copy2(info['path'], destination)
        if digest(destination) != info['sha256']:
            raise RuntimeError('فایل هنگام پردازش تغییر کرده است؛ دوباره تلاش کنید.')
        checks = release_check(info, profile)
        report = {'created': datetime.now().astimezone().isoformat(), 'info': info,
                  'signature': checks['signature'], 'warnings': checks['warnings'],
                  'client': profile['client'], 'project': profile['project'], 'notes': notes,
                  'archived_file': str(destination)}
        write_json(folder / 'release.json', report)
        (folder / 'CHANGELOG.txt').write_text(notes or 'No release notes provided.', encoding='utf-8')
        (folder / 'SHA256SUMS.txt').write_text(info['sha256'] + '  ' + destination.name + '\n', encoding='utf-8')
        return folder
    except Exception:
        # Roll back only the unique release folder created by this invocation.
        shutil.rmtree(folder)
        raise


def compare_apks(first, second):
    differences = {}
    for key in ('package', 'version', 'code', 'bytes', 'min_sdk', 'target_sdk', 'architectures', 'debuggable'):
        if first[key] != second[key]:
            differences[key] = {'first': first[key], 'second': second[key]}
    differences['permissions_added'] = sorted(set(second['permissions']) - set(first['permissions']))
    differences['permissions_removed'] = sorted(set(first['permissions']) - set(second['permissions']))
    for label, info in [('first_signature', first), ('second_signature', second)]:
        try:
            differences[label] = signature(info['path'])
        except Exception as error:
            differences[label] = {'error': str(error)}
    left, right = differences['first_signature'], differences['second_signature']
    differences['same_certificates'] = (left['certificates'] == right['certificates']) if 'certificates' in left and 'certificates' in right else None
    return differences


def integrate(remove=False):
    import winreg
    base = r'Software\Classes\SystemFileAssociations\.apk\shell\APKDesk'
    def delete_tree(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                children = []
                i = 0
                while True:
                    try:
                        children.append(winreg.EnumKey(key, i)); i += 1
                    except OSError:
                        break
            for child in children:
                delete_tree(path + '\\' + child)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass
    delete_tree(base)
    if remove:
        return
    executable = Path(sys.executable).resolve()
    command = f'"{executable}"' if getattr(sys, 'frozen', False) else f'"{executable}" "{Path(__file__).resolve()}"'
    def set_values(path, values):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            for name, value in values.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    set_values(base, {'MUIVerb': 'APK Desk', 'SubCommands': '', 'Icon': str(executable)})
    for index, (action, label) in enumerate(ACTIONS):
        path = base + '\\shell\\' + f'{index:02d}_{action}'
        set_values(path, {'MUIVerb': label, 'MultiSelectModel': 'Single'})
        set_values(path + '\\command', {'': command + f' --action {action} "%1"'})


class APKDesk(BaseWindow):
    def __init__(self, files=(), action='open'):
        super().__init__()
        self.title('APK Desk — مدیریت نسخه و تحویل اپلیکیشن')
        self.geometry('1100x760')
        self.minsize(1000, 650)
        self.settings = read_settings()
        self.files = []
        self.events = queue.Queue()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.busy = False
        self.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TButton', padding=6)
        top = ttk.Frame(self, padding=12); top.pack(fill='x')
        ttk.Label(top, text='APK Desk', font=('Segoe UI', 20, 'bold')).pack(side='left')
        ttk.Button(top, text='حذف منوی راست‌کلیک', command=lambda: self.integration(True)).pack(side='right')
        ttk.Button(top, text='نصب منوی راست‌کلیک', command=self.integration).pack(side='right', padx=5)
        form = ttk.LabelFrame(self, text='پروفایل پروژه / Project profile', padding=10); form.pack(fill='x', padx=12)
        self.profile_name = tk.StringVar(value=self.settings['active'])
        self.combo = ttk.Combobox(form, textvariable=self.profile_name, values=list(self.settings['profiles']), width=18)
        self.combo.grid(row=0, column=0, padx=4)
        self.combo.bind('<<ComboboxSelected>>', lambda e: self.load_profile())
        self.vars = {}
        for col, (key, label) in enumerate([('client','مشتری'), ('project','پروژه'), ('pattern','الگوی نام')], 1):
            ttk.Label(form, text=label).grid(row=0, column=col*2-1, padx=4)
            self.vars[key] = tk.StringVar()
            ttk.Entry(form, textvariable=self.vars[key], width=25 if key == 'pattern' else 13).grid(row=0,column=col*2)
        ttk.Label(form, text='پوشه آرشیو').grid(row=1,column=0,pady=8)
        self.vars['archive'] = tk.StringVar()
        ttk.Entry(form, textvariable=self.vars['archive'], width=65).grid(row=1,column=1,columnspan=4,sticky='ew')
        ttk.Button(form, text='انتخاب', command=self.choose_archive).grid(row=1,column=5)
        ttk.Button(form, text='ذخیره پروفایل', command=self.save_profile).grid(row=1,column=6)
        ttk.Label(form, text='Fields: {app} {version} {code} {package} {date} {client} {project}').grid(row=2,column=0,columnspan=7)
        self.load_profile()
        bar = ttk.Frame(self, padding=10); bar.pack(fill='x')
        ttk.Button(bar, text='افزودن APK', command=self.pick_files).pack(side='left')
        ttk.Button(bar, text='افزودن پوشه', command=self.pick_folder).pack(side='left',padx=4)
        ttk.Button(bar, text='پاک‌کردن فهرست', command=self.clear).pack(side='left')
        ttk.Button(bar, text='بازکردن آرشیو', command=self.open_archive).pack(side='right')
        self.listbox = tk.Listbox(self, height=5, selectmode='extended', font=('Segoe UI', 10))
        self.listbox.pack(fill='x',padx=12)
        if DND_FILES:
            self.listbox.drop_target_register(DND_FILES)
            self.listbox.dnd_bind('<<Drop>>', lambda e: self.add_files(self.tk.splitlist(e.data)))
        ttk.Label(self, text='فایل‌ها را اینجا رها کنید؛ عملیات روی موارد انتخاب‌شده یا تمام فهرست انجام می‌شود.').pack()
        actions = ttk.Frame(self, padding=10); actions.pack(fill='x')
        self.buttons = []
        for index, (name, label) in enumerate(ACTIONS[:-1]):
            button = ttk.Button(actions, text=label.split(' / ')[0], command=lambda a=name: self.execute(a))
            button.grid(row=index//4,column=index%4,padx=3,pady=3,sticky='ew'); self.buttons.append(button)
        for col in range(4):
            actions.grid_columnconfigure(col, weight=1)
        extra = ttk.Frame(self, padding=(12,0)); extra.pack(fill='x')
        ttk.Button(extra, text='برگرداندن آخرین تغییر نام', command=self.undo).pack(side='left')
        ttk.Label(extra, text='یادداشت انتشار:').pack(side='left',padx=8)
        self.notes = ttk.Entry(extra); self.notes.pack(side='left',fill='x',expand=True)
        self.status = tk.StringVar(value='آماده')
        ttk.Label(self, textvariable=self.status, padding=8).pack(fill='x')
        frame = ttk.Frame(self); frame.pack(fill='both',expand=True,padx=12,pady=(0,12))
        self.output = tk.Text(frame, wrap='word', font=('Segoe UI', 10), state='disabled')
        scroll = ttk.Scrollbar(frame, command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y'); self.output.pack(fill='both',expand=True)
        self.after(100, self.poll)
        self.add_files(files)
        if action != 'open':
            self.after(200, lambda: self.execute(action))

    def close(self):
        if self.busy:
            messagebox.showinfo('APK Desk', 'عملیات در حال اجراست؛ تا پایان آن صبر کنید.')
            return
        self.pool.shutdown(wait=False)
        self.destroy()

    def profile(self):
        return {key: var.get().strip() for key,var in self.vars.items()}

    def load_profile(self):
        profile = self.settings['profiles'].get(self.profile_name.get())
        if profile:
            for key,var in self.vars.items():
                var.set(profile[key])

    def save_profile(self):
        try:
            name = self.profile_name.get().strip()
            profile = self.profile()
            if not name or not profile['archive'] or not profile['pattern']:
                raise ValueError('نام پروفایل، الگو و پوشهٔ آرشیو الزامی هستند.')
            if not Path(profile['archive']).expanduser().is_absolute():
                raise ValueError('مسیر آرشیو باید مطلق باشد.')
            filename({'app':'App','version':'1','code':'1','package':'com.example'}, profile)
            self.settings['active'] = name
            self.settings['profiles'][name] = profile
            write_json(CONFIG, self.settings)
            self.combo.configure(values=list(self.settings['profiles']))
            self.status.set('پروفایل ذخیره شد؛ گزینه‌های راست‌کلیک از همین پروفایل استفاده می‌کنند.')
        except Exception as error:
            messagebox.showerror('APK Desk', str(error))

    def choose_archive(self):
        path = filedialog.askdirectory()
        if path:
            self.vars['archive'].set(path)

    def open_archive(self):
        try:
            root = Path(self.profile()['archive']).expanduser()
            root.mkdir(parents=True, exist_ok=True)
            os.startfile(root)
        except Exception as error:
            messagebox.showerror('APK Desk',str(error))

    def integration(self, remove=False):
        try:
            integrate(remove)
            messagebox.showinfo('APK Desk', 'منوی راست‌کلیک حذف شد.' if remove else 'منوی APK Desk نصب شد. در ویندوز ۱۱ ممکن است زیر Show more options باشد.')
        except Exception as error:
            messagebox.showerror('APK Desk', str(error))

    def add_files(self, paths):
        if self.busy:
            return
        for path in paths:
            path = str(Path(path).resolve())
            if Path(path).suffix.lower() == '.apk' and path not in self.files:
                self.files.append(path); self.listbox.insert('end',path)

    def pick_files(self):
        self.add_files(filedialog.askopenfilenames(filetypes=[('Android APK','*.apk')]))

    def pick_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.add_files(sorted(Path(folder).glob('*.apk')))

    def clear(self):
        if not self.busy:
            self.files.clear(); self.listbox.delete(0,'end')

    def log(self, text):
        self.output.configure(state='normal')
        self.output.insert('end',text + '\n\n'); self.output.see('end')
        self.output.configure(state='disabled')

    def execute(self, action):
        if self.busy:
            return
        indices = self.listbox.curselection()
        paths = [self.files[i] for i in indices] if indices else list(self.files)
        if not paths:
            self.pick_files(); paths = list(self.files)
        if not paths:
            return
        if action == 'compare' and len(paths) == 1:
            second = filedialog.askopenfilename(title='نسخهٔ دوم',filetypes=[('APK','*.apk')])
            if not second:
                return
            paths.append(second)
        if action == 'compare' and len(paths) != 2:
            messagebox.showerror('APK Desk','برای مقایسه دقیقاً دو فایل انتخاب کنید.'); return
        profile, notes = self.profile(), self.notes.get()
        if action != 'install' and not profile['archive']:
            messagebox.showerror('APK Desk','پوشهٔ آرشیو را تعیین کنید.'); return
        self.busy = True
        for button in self.buttons:
            button.configure(state='disabled')
        self.status.set('در حال پردازش…')
        if action == 'install':
            self.status.set('در حال جست‌وجوی گوشی و شبیه‌ساز…')
            self.pool.submit(self.prepare_install, paths)
        else:
            self.pool.submit(self.work, action, paths, profile, notes)

    def prepare_install(self, paths):
        try:
            self.events.put(('devices', (paths, connected_devices())))
        except Exception as error:
            self.events.put(('log', 'خطا: ' + str(error)))
            self.events.put(('done', None))

    def choose_device(self, paths, devices):
        def start(serial):
            self.status.set('در حال نصب روی دستگاه انتخاب‌شده…')
            self.pool.submit(self.work, 'install', paths, {}, '', serial)
        if len(devices) == 1:
            start(devices[0]['serial'])
            return
        dialog = tk.Toplevel(self)
        dialog.title('انتخاب دستگاه / Select device')
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(dialog, text='APK روی کدام گوشی یا شبیه‌ساز نصب شود؟', padding=12).pack()
        choice = ttk.Combobox(dialog, values=[d['label'] for d in devices], state='readonly', width=75)
        choice.pack(padx=12,pady=8)
        choice.current(0)
        def accept():
            serial = devices[choice.current()]['serial']
            dialog.destroy()
            start(serial)
        def cancel():
            dialog.destroy()
            self.events.put(('log', 'نصب لغو شد.'))
            self.events.put(('done', None))
        buttons = ttk.Frame(dialog, padding=12); buttons.pack()
        ttk.Button(buttons, text='نصب', command=accept).pack(side='left',padx=5)
        ttk.Button(buttons, text='انصراف', command=cancel).pack(side='left')
        dialog.protocol('WM_DELETE_WINDOW', cancel)

    def work(self, action, paths, profile, notes, device=None):
        try:
            if action == 'compare':
                result = compare_apks(inspect_apk(paths[0]), inspect_apk(paths[1]))
                self.events.put(('log',json.dumps(result,ensure_ascii=False,indent=2)))
                return
            for path in paths:
                try:
                    if action == 'install':
                        self.events.put(('log', 'در حال نصب: ' + str(path)))
                        result = install_apk(path, device)
                        self.events.put(('log', str(path) + '\n' + result))
                        continue
                    info = inspect_apk(path)
                    if action == 'info':
                        result = json.dumps(info,ensure_ascii=False,indent=2)
                    elif action == 'signature':
                        result = signature(path)['details']
                    elif action == 'check':
                        result = json.dumps(release_check(info,profile),ensure_ascii=False,indent=2)
                    elif action == 'rename':
                        target = Path(path).with_name(filename(info,profile))
                        if os.path.normcase(str(target)) == os.path.normcase(str(Path(path))):
                            result = 'نام فایل از قبل مطابق الگو است.'
                        elif target.exists():
                            raise FileExistsError('فایل مقصد موجود است؛ هیچ فایلی جایگزین نشد: ' + str(target))
                        else:
                            record = DATA / 'undo' / (datetime.now().strftime('%Y%m%d%H%M%S%f') + '.json')
                            write_json(record, {'old': path, 'new': str(target), 'sha256': info['sha256']})
                            try:
                                Path(path).rename(target)
                            except Exception:
                                record.unlink(); raise
                            self.events.put(('renamed',(path,str(target))))
                            result = 'تغییر نام انجام شد: ' + str(target)
                    elif action in ('archive','bundle'):
                        folder = archive_apk(info,profile,notes)
                        if action == 'bundle':
                            target = Path(str(folder) + '.zip')
                            with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as bundle:
                                for item in folder.iterdir():
                                    bundle.write(item,item.name)
                            result = 'بستهٔ تحویل: ' + str(target)
                        else:
                            result = 'آرشیو ثبت شد: ' + str(folder)
                    self.events.put(('log',str(path) + '\n' + result))
                except Exception as error:
                    self.events.put(('log','خطا: ' + str(path) + '\n' + str(error)))
        except Exception as error:
            self.events.put(('log','خطا: ' + str(error)))
        finally:
            self.events.put(('done',None))

    def undo(self):
        if self.busy:
            return
        try:
            records = sorted((DATA/'undo').glob('*.json'),reverse=True)
            if not records:
                raise ValueError('تغییر نامی برای برگشت وجود ندارد.')
            record = records[0]
            item = json.loads(record.read_text(encoding='utf-8'))
            old,new = Path(item['old']),Path(item['new'])
            if old.exists() or not new.is_file() or digest(new) != item['sha256']:
                raise ValueError('وضعیت فایل تغییر کرده است؛ برگشت خودکار ممکن نیست.')
            new.rename(old); record.unlink()
            if str(new) in self.files:
                self.files[self.files.index(str(new))] = str(old)
                self.listbox.delete(0,'end')
                for path in self.files:
                    self.listbox.insert('end',path)
            self.log('نام قبلی بازیابی شد: ' + str(old))
        except Exception as error:
            messagebox.showerror('APK Desk',str(error))

    def poll(self):
        try:
            while True:
                event,value = self.events.get_nowait()
                if event == 'log':
                    self.log(value)
                elif event == 'devices':
                    self.choose_device(*value)
                elif event == 'renamed':
                    old,new = value
                    if old in self.files:
                        self.files[self.files.index(old)] = new
                        self.listbox.delete(0,'end')
                        for path in self.files:
                            self.listbox.insert('end',path)
                elif event == 'done':
                    self.busy = False; self.status.set('پردازش پایان یافت؛ نتیجهٔ هر فایل در گزارش زیر است.')
                    for button in self.buttons:
                        button.configure(state='normal')
        except queue.Empty:
            pass
        self.after(100,self.poll)


def main():
    parser = argparse.ArgumentParser(description='APK Desk')
    parser.add_argument('files',nargs='*')
    parser.add_argument('--action',choices=[x[0] for x in ACTIONS],default='open')
    parser.add_argument('--install-menu',action='store_true')
    parser.add_argument('--remove-menu',action='store_true')
    args = parser.parse_args()
    if args.install_menu or args.remove_menu:
        integrate(args.remove_menu)
        return
    app = APKDesk(args.files,args.action)
    app.mainloop()


if __name__ == '__main__':
    main()
