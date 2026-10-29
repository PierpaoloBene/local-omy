import 'dart:async';

import 'package:flutter/cupertino.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';

import '../features.dart';
import '../services/ble_service.dart';
import '../settings.dart';
import '../upload/uploader.dart';
import 'common.dart';

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
      'always' => 'Posizione approssimata: in Impostazioni attiva "Posizione esatta".',
      'when_in_use' =>
        'iOS dà la posizione solo mentre l\'app è aperta, quindi le registrazioni in tasca restano '
            'senza luogo. In Impostazioni scegli "Sempre" e attiva "Posizione esatta".',
      'not_determined' => 'Permesso non ancora chiesto: riapri l\'app.',
      _ => 'Posizione negata: in Impostazioni scegli "Sempre" e attiva "Posizione esatta".',
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
      onError: (e) => mounted ? showMessage(context, e, title: 'Ricerca non riuscita') : null,
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
      setState(() => _urlError = 'Serve un URL https://');
      return;
    }
    widget.settings.macUrl = url;
    if (_token.text.isNotEmpty) await widget.settings.setToken(_token.text.trim());
    _token.clear();
    setState(() => _urlError = null);
    showMessage(context, 'URL e token salvati.', title: 'Salvato');
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
        ? 'Sincronizzazione con il Mac non riuscita: ${f.lastSyncError}'
        : f.lastSyncAt == null
        ? 'Non ancora sincronizzate con il Mac.'
        : 'Sincronizzate con il Mac alle ${two(f.lastSyncAt!.hour)}:${two(f.lastSyncAt!.minute)}. '
              'Spegnere una funzione interrompe la raccolta, ma non cancella quello che c\'è già.';
    return [
      for (final (device, title) in [('iphone', 'Funzioni sull\'iPhone'), ('mac', 'Funzioni sul Mac')])
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
                title: 'Posizione in background',
                subtitle: _locationProblem(),
                trailing: CupertinoButton(
                  padding: EdgeInsets.zero,
                  onPressed: f.openSettings,
                  child: const Text('Apri'),
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
      title: 'Impostazioni',
      previousPageTitle: 'Oggi',
      slivers: [
        SliverList.list(
          children: [
            Section(
              header: 'Ciondolo',
              children: [
                IosRow(
                  leading: const IconSquare(CupertinoIcons.bluetooth, CupertinoColors.systemBlue),
                  title: saved == null ? 'Nessuno scelto' : 'Omi',
                  subtitle: saved,
                ),
                IosRow(title: 'Cerca ciondoli', onTap: _startScan),
                for (final r in _results)
                  IosRow(
                    title: r.device.platformName,
                    subtitle: '${r.device.remoteId.str} · ${r.rssi} dBm',
                    trailing: r.device.remoteId.str == saved
                        ? Icon(CupertinoIcons.checkmark_alt, color: resolve(context, tint))
                        : null,
                    onTap: () => _choose(r),
                  ),
                if (saved != null) IosRow(title: 'Dimentica questo ciondolo', destructive: true, onTap: _forget),
              ],
            ),
            const SizedBox(height: 12),
            CupertinoFormSection.insetGrouped(
              header: const Text('MAC'),
              footer: Text(
                _urlError ??
                    (widget.settings.token == null
                        ? 'Token non impostato.'
                        : 'Token impostato: lascia vuoto il campo per non cambiarlo.'),
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
                  placeholder: widget.settings.token == null ? 'Incolla il token' : '••••••••',
                  obscureText: true,
                  autocorrect: false,
                  enableSuggestions: false,
                ),
              ],
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: CupertinoButton.filled(onPressed: _save, child: const Text('Salva')),
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
