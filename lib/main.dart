import 'package:flutter/material.dart';

import 'recording/recorder.dart';
import 'recording/segment_store.dart';
import 'services/ble_service.dart';
import 'settings.dart';
import 'upload/uploader.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await BleService.init();
  final settings = await Settings.load();
  final ble = BleService();
  final store = await SegmentStore.open();
  final recorder = Recorder(ble, store)..start();
  final uploader = Uploader(store, settings)..start();
  recorder.onSegmentClosed = uploader.trigger;

  final deviceId = settings.deviceId;
  if (deviceId != null) ble.connect(deviceId);

  runApp(MaterialApp(
    home: Scaffold(
      body: Center(
        child: ListenableBuilder(
          listenable: Listenable.merge([recorder, uploader]),
          builder: (_, _) => Text('local-omy\n${ble.state.name}\nrecording: ${recorder.recording}\nqueued: ${uploader.queued}'),
        ),
      ),
    ),
  ));
}
