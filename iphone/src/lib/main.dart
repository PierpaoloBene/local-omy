import 'package:flutter/material.dart';

import 'features.dart';
import 'recording/recorder.dart';
import 'recording/segment_store.dart';
import 'services/ble_service.dart';
import 'settings.dart';
import 'ui/home_page.dart';
import 'upload/uploader.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await BleService.init();
  final settings = await Settings.load();
  final ble = BleService();
  final features = await Features.load(settings);
  final store = await SegmentStore.open()
    ..feature = features.on
    ..battery = () => ble.battery;
  final recorder = Recorder(ble, store)..start();
  final uploader = Uploader(store, settings)..start();
  recorder.onSegmentClosed = uploader.trigger;
  features.start();

  final deviceId = settings.deviceId;
  if (deviceId != null) ble.connect(deviceId);

  runApp(
    MaterialApp(
      title: 'local-omy',
      theme: ThemeData(colorSchemeSeed: Colors.teal),
      darkTheme: ThemeData(colorSchemeSeed: Colors.teal, brightness: Brightness.dark),
      home: HomePage(ble: ble, recorder: recorder, uploader: uploader, settings: settings, features: features),
    ),
  );
}
