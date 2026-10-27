import 'package:flutter/material.dart';

import 'recording/recorder.dart';
import 'recording/segment_store.dart';
import 'services/ble_service.dart';
import 'settings.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await BleService.init();
  final settings = await Settings.load();
  final ble = BleService();
  final recorder = Recorder(ble, await SegmentStore.open())..start();

  final deviceId = settings.deviceId;
  if (deviceId != null) ble.connect(deviceId);

  runApp(MaterialApp(
    home: Scaffold(
      body: Center(
        child: ListenableBuilder(
          listenable: recorder,
          builder: (_, _) => Text('local-omy\n${ble.state.name}\nrecording: ${recorder.recording}'),
        ),
      ),
    ),
  ));
}
