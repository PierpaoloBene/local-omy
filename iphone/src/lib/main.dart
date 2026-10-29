import 'package:flutter/cupertino.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import 'api/mac_api.dart';
import 'features.dart';
import 'l10n.dart';
import 'recording/recorder.dart';
import 'recording/segment_store.dart';
import 'services/ble_service.dart';
import 'settings.dart';
import 'ui/app_shell.dart';
import 'ui/common.dart';
import 'upload/uploader.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await BleService.init();
  final settings = await Settings.load();
  final ble = BleService();
  final features = await Features.load(settings);
  final store = await SegmentStore.open()
    ..feature = features.on
    ..battery = (() => ble.battery)
    ..firmware = (() => ble.firmware);
  final recorder = Recorder(ble, store)..start();
  final uploader = Uploader(store, settings)..start();
  recorder.onSegmentClosed = uploader.trigger;
  features.start();

  final deviceId = settings.deviceId;
  if (deviceId != null) ble.connect(deviceId);

  runApp(
    CupertinoApp(
      title: 'local-omy',
      debugShowCheckedModeBanner: false,
      theme: const CupertinoThemeData(primaryColor: tint),
      // Italian or English, after the iPhone (l10n.dart).
      locale: Locale(english ? 'en' : 'it'),
      supportedLocales: const [Locale('it'), Locale('en')],
      localizationsDelegates: GlobalMaterialLocalizations.delegates,
      home: AppShell(
        ble: ble,
        recorder: recorder,
        uploader: uploader,
        settings: settings,
        features: features,
        api: MacApi(settings),
      ),
    ),
  );
}
