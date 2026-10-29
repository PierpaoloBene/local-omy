import 'dart:async';

import 'package:flutter/cupertino.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';

import '../features.dart';
import '../services/ble_service.dart';
import '../settings.dart';
import '../upload/uploader.dart';
import 'common.dart';
import '../l10n.dart';

class SettingsPage extends StatefulWidget {
  final BleService ble;
  final Settings settings;
  final Uploader uploader;
  final Features features;

  const SettingsPage({
    super.key,
    required this.ble,
    required this.settings,
    required this.uploader,
    required this.features,
  });

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> with WidgetsBindingObserver {
  late final _url = TextEditingController(text: widget.settings.macUrl ?? '');
  final _token = TextEditingController();
  StreamSubscription<List<ScanResult>>? _scan;
  List<ScanResult> _results = [];
  String? _urlError;
  ({String auth, bool precise})? _location;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _checkLocation();
  }

  /// Back from the iOS Settings app: the permission may have changed.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _checkLocation();
  }

  Future<void> _checkLocation() async {
    final s = await widget.features.locationStatus();
    if (mounted) setState(() => _location = s);
  }

  /// Why the location can't work in background, or null if it can.
  String? _locationProblem() {
    final s = _location;
    if (s == null || !widget.features.on('location')) return null;
    return switch (s.auth) {
      'always' when s.precise => null,
      'always' => tr(
        'Posizione approssimata: in Impostazioni attiva "Posizione esatta".',
        'Approximate location: turn on "Precise Location" in Settings.',
      ),
      'when_in_use' => tr(
        'iOS dà la posizione solo mentre l\'app è aperta, quindi le registrazioni in tasca restano '
            'senza luogo. In Impostazioni scegli "Sempre" e attiva "Posizione esatta".',
        'iOS gives the location only while the app is open, so recordings made with the phone in your '
            'pocket have no place. In Settings choose "Always" and turn on "Precise Location".',
      ),
      'not_determined' => tr(
        'Permesso non ancora chiesto: riapri l\'app.',
        'Permission not asked yet: reopen the app.',
      ),
      _ => tr(
        'Posizione negata: in Impostazioni scegli "Sempre" e attiva "Posizione esatta".',
        'Location denied: in Settings choose "Always" and turn on "Precise Location".',
      ),
    };
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _scan?.cancel();
    widget.ble.stopScan();
    _url.dispose();
    _token.dispose();
    super.dispose();
  }

  void _startScan() {
    _scan?.cancel();
    setState(() => _results = []);
    _scan = widget.ble.scan().listen(
      (r) => setState(() => _results = r),
      onError: (e) => mounted ? showMessage(context, e, title: tr('Ricerca non riuscita', 'Search failed')) : null,
    );
  }

  Future<void> _choose(ScanResult r) async {
    await widget.ble.stopScan();
    widget.settings.deviceId = r.device.remoteId.str;
    await widget.ble.connect(r.device.remoteId.str);
    setState(() {});
  }

  Future<void> _forget() async {
    widget.settings.deviceId = null;
    await widget.ble.forget();
    setState(() {});
  }

  Future<void> _save() async {
    final url = _url.text.trim().replaceAll(RegExp(r'/+$'), '');
    final uri = Uri.tryParse(url);
    if (url.isNotEmpty && (uri == null || uri.scheme != 'https' || uri.host.isEmpty)) {
      setState(() => _urlError = tr('Serve un URL https://', 'An https:// URL is needed'));
      return;
    }
    widget.settings.macUrl = url;
    if (_token.text.isNotEmpty) await widget.settings.setToken(_token.text.trim());
    _token.clear();
    setState(() => _urlError = null);
    showMessage(context, tr('URL e token salvati.', 'URL and token saved.'), title: tr('Salvato', 'Saved'));
    widget.uploader.trigger();
    widget.features.sync();
  }

  static const _icons = <String, (IconData, Color)>{
    'location': (CupertinoIcons.location_fill, CupertinoColors.systemBlue),
    'calls': (CupertinoIcons.phone_fill, CupertinoColors.systemGreen),
    'battery': (CupertinoIcons.battery_full, CupertinoColors.systemGreen),
    'mac_audio': (CupertinoIcons.speaker_2_fill, CupertinoColors.systemPink),
    'diarization': (CupertinoIcons.person_2_fill, CupertinoColors.systemOrange),
    'voice_prints': (CupertinoIcons.waveform, CupertinoColors.systemPurple),
    'auto_transcribe': (CupertinoIcons.text_bubble_fill, CupertinoColors.systemIndigo),
  };

  List<Widget> _features(BuildContext context) {
    final f = widget.features;
    String two(int v) => v.toString().padLeft(2, '0');
    final sync = f.lastSyncError != null
        ? tr(
            'Sincronizzazione con il Mac non riuscita: ${f.lastSyncError}',
            'Sync with the Mac failed: ${f.lastSyncError}',
          )
        : f.lastSyncAt == null
        ? tr('Non ancora sincronizzate con il Mac.', 'Not synced with the Mac yet.')
        : tr(
            'Sincronizzate con il Mac alle ${two(f.lastSyncAt!.hour)}:${two(f.lastSyncAt!.minute)}. '
                'Spegnere una funzione interrompe la raccolta, ma non cancella quello che c\'è già.',
            'Synced with the Mac at ${two(f.lastSyncAt!.hour)}:${two(f.lastSyncAt!.minute)}. '
                'Turning a feature off stops collecting, but does not delete what is already there.',
          );
    return [
      for (final (device, title) in [
        ('iphone', tr('Funzioni sull\'iPhone', 'Features on the iPhone')),
        ('mac', tr('Funzioni sul Mac', 'Features on the Mac')),
      ])
        Section(
          header: title,
          footer: device == 'mac' ? sync : null,
          children: [
            for (final MapEntry(key: key, value: (d, name, description)) in featureInfo.entries)
              if (d == device)
                IosRow(
                  leading: IconSquare(
                    _icons[key]?.$1 ?? CupertinoIcons.gear_solid,
                    _icons[key]?.$2 ?? CupertinoColors.systemGrey,
                  ),
                  title: name,
                  subtitle: description,
                  trailing: CupertinoSwitch(value: f.on(key), onChanged: (v) => f.set(key, v)),
                ),
            if (device == 'iphone' && _locationProblem() != null)
              IosRow(
                leading: const IconSquare(CupertinoIcons.location_slash_fill, CupertinoColors.systemRed),
                title: tr('Posizione in background', 'Location in background'),
                subtitle: _locationProblem(),
                trailing: CupertinoButton(
                  padding: EdgeInsets.zero,
                  onPressed: f.openSettings,
                  child: Text(tr('Apri', 'Open')),
                ),
              ),
          ],
        ),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final saved = widget.settings.deviceId;
    return TabScaffold(
      title: tr('Impostazioni', 'Settings'),
      previousPageTitle: tr('Oggi', 'Today'),
      slivers: [
        SliverList.list(
          children: [
            Section(
              header: tr('Ciondolo', 'Pendant'),
              children: [
                IosRow(
                  leading: const IconSquare(CupertinoIcons.bluetooth, CupertinoColors.systemBlue),
                  title: saved == null ? tr('Nessuno scelto', 'None chosen') : 'Omi',
                  subtitle: saved,
                ),
                IosRow(title: tr('Cerca ciondoli', 'Find pendants'), onTap: _startScan),
                for (final r in _results)
                  IosRow(
                    title: r.device.platformName,
                    subtitle: '${r.device.remoteId.str} · ${r.rssi} dBm',
                    trailing: r.device.remoteId.str == saved
                        ? Icon(CupertinoIcons.checkmark_alt, color: resolve(context, tint))
                        : null,
                    onTap: () => _choose(r),
                  ),
                if (saved != null)
                  IosRow(
                    title: tr('Dimentica questo ciondolo', 'Forget this pendant'),
                    destructive: true,
                    onTap: _forget,
                  ),
              ],
            ),
            const SizedBox(height: 12),
            CupertinoFormSection.insetGrouped(
              header: const Text('MAC'),
              footer: Text(
                _urlError ??
                    (widget.settings.token == null
                        ? tr('Token non impostato.', 'Token not set.')
                        : tr(
                            'Token impostato: lascia vuoto il campo per non cambiarlo.',
                            'Token set: leave the field empty to keep it.',
                          )),
                style: Ios.footnote(
                  context,
                ).copyWith(color: _urlError == null ? null : resolve(context, CupertinoColors.systemRed)),
              ),
              children: [
                CupertinoTextFormFieldRow(
                  controller: _url,
                  prefix: const SizedBox(width: 60, child: Text('URL')),
                  placeholder: 'https://mac.tailnet.ts.net',
                  keyboardType: TextInputType.url,
                  autocorrect: false,
                ),
                CupertinoTextFormFieldRow(
                  controller: _token,
                  prefix: const SizedBox(width: 60, child: Text('Token')),
                  placeholder: widget.settings.token == null ? tr('Incolla il token', 'Paste the token') : '••••••••',
                  obscureText: true,
                  autocorrect: false,
                  enableSuggestions: false,
                ),
              ],
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: CupertinoButton.filled(onPressed: _save, child: Text(tr('Salva', 'Save'))),
            ),
            const SizedBox(height: 12),
            ListenableBuilder(
              listenable: widget.features,
              builder: (context, _) => Column(children: _features(context)),
            ),
          ],
        ),
      ],
    );
  }
}
