import 'package:flutter/material.dart';

import '../recording/recorder.dart';
import '../services/ble_service.dart';
import '../settings.dart';
import '../upload/uploader.dart';
import 'settings_page.dart';

class HomePage extends StatelessWidget {
  final BleService ble;
  final Recorder recorder;
  final Uploader uploader;
  final Settings settings;

  const HomePage({
    super.key,
    required this.ble,
    required this.recorder,
    required this.uploader,
    required this.settings,
  });

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('local-omy'),
        actions: [
          IconButton(
            icon: const Icon(Icons.settings),
            onPressed: () => Navigator.of(context).push(
              MaterialPageRoute(
                builder: (_) => SettingsPage(ble: ble, settings: settings, uploader: uploader),
              ),
            ),
          ),
        ],
      ),
      body: ListenableBuilder(
        listenable: Listenable.merge([recorder, uploader]),
        builder: (context, _) {
          final error = _latestError();
          return ListView(
            children: [
              ListTile(
                leading: const Icon(Icons.bluetooth),
                title: const Text('Ciondolo'),
                subtitle: Text(settings.deviceId == null ? 'Nessuno scelto' : _pendantLabel()),
              ),
              ListTile(
                leading: Icon(
                  recorder.recording ? Icons.fiber_manual_record : Icons.stop_circle_outlined,
                  color: recorder.recording ? Colors.red : null,
                ),
                title: const Text('Registrazione'),
                subtitle: Text(recorder.recording ? 'Attiva' : 'Ferma'),
              ),
              ListTile(
                leading: const Icon(Icons.cloud_upload_outlined),
                title: const Text('Segmenti in coda'),
                subtitle: Text('${uploader.queued}${uploader.uploading ? ' (invio in corso)' : ''}'),
                trailing: IconButton(icon: const Icon(Icons.refresh), onPressed: uploader.trigger),
              ),
              ListTile(
                leading: const Icon(Icons.check_circle_outline),
                title: const Text('Ultimo upload riuscito'),
                subtitle: Text(uploader.lastSuccessAt == null ? '—' : _fmt(uploader.lastSuccessAt!)),
              ),
              ListTile(
                leading: const Icon(Icons.error_outline),
                title: const Text('Ultimo errore'),
                subtitle: Text(error == null ? '—' : '${_fmt(error.$1)}\n${error.$2}'),
                isThreeLine: error != null,
              ),
            ],
          );
        },
      ),
    );
  }

  String _pendantLabel() {
    switch (ble.state) {
      case PendantState.streaming:
        return 'Connesso · batteria ${ble.battery == null ? '?' : '${ble.battery}%'}';
      case PendantState.connecting:
        return 'Non connesso (in attesa)';
      case PendantState.disconnected:
        return 'Non connesso';
    }
  }

  (DateTime, String)? _latestError() {
    final candidates = [
      if (recorder.lastErrorAt != null) (recorder.lastErrorAt!, recorder.lastError!),
      if (uploader.lastErrorAt != null) (uploader.lastErrorAt!, uploader.lastError!),
    ]..sort((a, b) => b.$1.compareTo(a.$1));
    return candidates.firstOrNull;
  }

  static String _fmt(DateTime t) {
    String two(int v) => v.toString().padLeft(2, '0');
    return '${two(t.day)}/${two(t.month)} ${two(t.hour)}:${two(t.minute)}:${two(t.second)}';
  }
}
