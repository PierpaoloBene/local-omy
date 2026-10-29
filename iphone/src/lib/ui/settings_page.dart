import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';

import '../features.dart';
import '../services/ble_service.dart';
import '../settings.dart';
import '../upload/uploader.dart';

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

class _SettingsPageState extends State<SettingsPage> {
  late final _url = TextEditingController(text: widget.settings.macUrl ?? '');
  final _token = TextEditingController();
  StreamSubscription<List<ScanResult>>? _scan;
  List<ScanResult> _results = [];
  String? _urlError;

  @override
  void dispose() {
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
      onError: (e) => _snack('Scan non riuscito: $e'),
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
    _snack('Salvato');
    widget.uploader.trigger();
    widget.features.sync();
  }

  Widget _featureList(BuildContext context) {
    final f = widget.features;
    final sync = f.lastSyncError != null
        ? 'Sincronizzazione con il Mac non riuscita: ${f.lastSyncError}'
        : f.lastSyncAt == null
        ? 'Non ancora sincronizzate con il Mac'
        : 'Sincronizzate con il Mac alle ${TimeOfDay.fromDateTime(f.lastSyncAt!).format(context)}';
    final small = Theme.of(context).textTheme.bodySmall;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(sync, style: small),
        for (final (device, title) in [('iphone', 'iPhone'), ('mac', 'Mac')]) ...[
          const SizedBox(height: 12),
          Text(title, style: Theme.of(context).textTheme.titleSmall),
          for (final MapEntry(key: key, value: (d, name, description)) in featureInfo.entries)
            if (d == device)
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(name),
                subtitle: Text(description),
                value: f.on(key),
                onChanged: (v) => f.set(key, v),
              ),
        ],
        const SizedBox(height: 12),
        Text('Spegnere una funzione interrompe la raccolta, ma non cancella quello che c\'è già.', style: small),
      ],
    );
  }

  void _snack(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  @override
  Widget build(BuildContext context) {
    final saved = widget.settings.deviceId;
    return Scaffold(
      appBar: AppBar(title: const Text('Impostazioni')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text('Ciondolo', style: Theme.of(context).textTheme.titleMedium),
          ListTile(
            contentPadding: EdgeInsets.zero,
            title: Text(saved ?? 'Nessuno scelto'),
            trailing: saved == null ? null : TextButton(onPressed: _forget, child: const Text('Dimentica')),
          ),
          OutlinedButton(onPressed: _startScan, child: const Text('Cerca ciondoli')),
          for (final r in _results)
            ListTile(
              title: Text(r.device.platformName),
              subtitle: Text('${r.device.remoteId.str} · ${r.rssi} dBm'),
              trailing: r.device.remoteId.str == saved ? const Icon(Icons.check) : null,
              onTap: () => _choose(r),
            ),
          const SizedBox(height: 24),
          Text('Mac', style: Theme.of(context).textTheme.titleMedium),
          TextField(
            controller: _url,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: InputDecoration(labelText: 'URL', hintText: 'https://mac.tailnet.ts.net', errorText: _urlError),
          ),
          TextField(
            controller: _token,
            obscureText: true,
            autocorrect: false,
            enableSuggestions: false,
            decoration: InputDecoration(
              labelText: 'Token',
              helperText: widget.settings.token == null
                  ? 'Non impostato'
                  : 'Impostato (lascia vuoto per non cambiarlo)',
            ),
          ),
          const SizedBox(height: 16),
          FilledButton(onPressed: _save, child: const Text('Salva')),
          const SizedBox(height: 24),
          Text('Funzioni', style: Theme.of(context).textTheme.titleMedium),
          ListenableBuilder(listenable: widget.features, builder: (context, _) => _featureList(context)),
        ],
      ),
    );
  }
}
