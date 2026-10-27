import 'package:flutter/material.dart';

import 'services/ble_service.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await BleService.init();
  runApp(const MaterialApp(home: Scaffold(body: Center(child: Text('local-omy')))));
}
