/// A MacApi answering from a function, with a configured Mac (fake URL and
/// token): no network, no Keychain.
library;

import 'dart:convert';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:local_omy/api/mac_api.dart';
import 'package:local_omy/settings.dart';
import 'package:shared_preferences/shared_preferences.dart';

Future<MacApi> fakeMac(Future<http.Response> Function(http.Request) answer) async {
  TestWidgetsFlutterBinding.ensureInitialized();
  SharedPreferences.setMockInitialValues({'mac_url': 'https://mac.test'});
  TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
    const MethodChannel('plugins.it_nomads.com/flutter_secure_storage'),
    (call) async => call.method == 'read' ? 'x' * 40 : null,
  );
  return MacApi(await Settings.load(), client: MockClient(answer));
}

http.Response json(Object body, [int status = 200]) =>
    http.Response.bytes(utf8.encode(jsonEncode(body)), status, headers: {'content-type': 'application/json'});

Map<String, Object?> conversation(String day, String session) => {
  'day': day,
  'session': session,
  'focus': '$session/0',
  'start': '${day}T10:00:00+01:00',
  'end': '${day}T10:30:00+01:00',
  'minutes': 30,
  'place': null,
  'app': null,
  'types': ['room'],
  'participants': [
    {'name': 'Io', 'person': 'me', 'type': 'room', 'minutes': 10.0},
  ],
  'snippet': 'ciao',
};
