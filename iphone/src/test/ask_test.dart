import 'dart:async';
import 'dart:convert';

import 'package:flutter/cupertino.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:local_omy/api/mac_api.dart';
import 'package:local_omy/api/models.dart';
import 'package:local_omy/l10n.dart';
import 'package:local_omy/settings.dart';
import 'package:local_omy/ui/ask_page.dart';

import 'fakes.dart';

Stream<List<int>> chunks(List<String> parts) => Stream.fromIterable([for (final p in parts) utf8.encode(p)]);

class FakeAsk extends MacApi {
  final StreamController<AskEvent> events = StreamController.broadcast();
  final asked = <String>[];
  FakeAsk(Settings s) : super(s);

  @override
  Stream<AskEvent> ask(String question, List<AskTurn> history, {Duration idle = MacApi.askIdle}) {
    asked.add(question);
    return events.stream;
  }
}

Map<String, Object?> cite(int n) => {
  'n': n,
  'kind': 'nota',
  'session': 's$n',
  'day': '2026-11-21',
  'time': '18:22',
  't': 1.0,
  'row_id': 's$n/0',
  'title': 'Titolo $n',
  'snippet': '',
};

Future<FakeAsk> pumpAsk(WidgetTester tester) async {
  await fakeMac((_) async => http.Response('', 404));
  final api = FakeAsk(await Settings.load());
  await tester.pumpWidget(CupertinoApp(home: AskPage(api: api)));
  return api;
}

Future<void> send(WidgetTester tester, String q) async {
  await tester.enterText(find.byKey(const Key('ask-input')), q);
  await tester.pump();
  await tester.tap(find.byKey(const Key('ask-send')));
  await tester.pump();
}

Finder richText(String s) =>
    find.byWidgetPredicate((w) => w is RichText && w.text.toPlainText().contains(s), description: 'text "$s"');

void main() {
  test('SSE: events split across chunk and line boundaries', () async {
    final events = await parseAskStream(
      chunks([
        'data: {"type":"status","stage":"wak',
        'ing"}\n\ndata: {"type":"delta","text":"Ci',
        'ao [1]"}\ndata: {"type":"delta","text":"\u00e8 \u00e0"}\n: keep-alive\n\ndata: {"type":"future"}\n',
        'data: {"type":"done","not_found":false,"timings":{"ttft_ms":1800,"total_ms":3400}}\n',
      ]),
    ).toList();
    expect(events.length, 4);
    expect((events[0] as AskStatus).stage, 'waking');
    expect((events[1] as AskDelta).text, 'Ciao [1]');
    expect((events[2] as AskDelta).text, '\u00e8 \u00e0');
    final done = events[3] as AskDone;
    expect(done.notFound, false);
    expect(done.timings['ttft_ms'], 1800);
  });

  test('SSE: several events in one chunk, citations and errors, utf8 cut mid-character', () async {
    final line = 'data: {"type":"citations","items":[${jsonEncode(cite(1))}]}\n'
        'data: {"type":"error","message":"boom"}\n';
    final bytes = utf8.encode('data: {"type":"delta","text":"\u00e8"}\n$line');
    final cut = bytes.indexOf(0xC3) + 1;
    final events = await parseAskStream(Stream.fromIterable([bytes.sublist(0, cut), bytes.sublist(cut)])).toList();
    expect((events[0] as AskDelta).text, '\u00e8');
    expect((events[1] as AskCitations).items.single.title, 'Titolo 1');
    expect((events[2] as AskError).message, 'boom');
  });

  test('note: 404 is null, a note is parsed; ask sends the question', () async {
    late http.Request seen;
    final api = await fakeMac((r) async {
      seen = r;
      if (r.url.path.endsWith('/missing')) return http.Response('', 404);
      if (r.url.path.startsWith('/v1/brain/note/')) {
        return json({
          'titolo': 'T',
          'riassunto': 'R',
          'punti_chiave': [
            {'testo': 'uno'},
          ],
          'impegni_e_todo': [
            {'chi': 'Io', 'cosa': 'chiamare'},
          ],
          'entita': {
            'persone': [
              {'nome': 'Marco'},
            ],
          },
        });
      }
      return http.Response('data: {"type":"delta","text":"x"}\n', 200);
    });
    expect(await api.note('missing'), isNull);
    final n = (await api.note('s1'))!;
    expect(n.titolo, 'T');
    expect(n.puntiChiave, ['uno']);
    expect(n.impegni.single, contains('chiamare'));
    expect(n.entita, ['Marco']);
    final ev = await api.ask('Ciao?', [for (var i = 0; i < 10; i++) AskTurn('user', '$i')]).toList();
    expect((ev.single as AskDelta).text, 'x');
    final body = jsonDecode(seen.body) as Map;
    expect(seen.url.path, '/v1/ask');
    expect(body['question'], 'Ciao?');
    expect((body['history'] as List).length, 8);
    expect(body['now'], matches(RegExp(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$')));
    expect(seen.headers['Authorization'], startsWith('Bearer '));
  });

  test('Citation tolerates nulls; one bad citation does not drop the event', () {
    final c = Citation.fromJson({'n': 2.0, 'session': null, 'day': null, 't': null});
    expect((c.n, c.session, c.day, c.t), (2, '', '', 0.0));
    expect(Citation.fromJson({...cite(1), 'session_day': '2026-11-20'}).sessionDay, '2026-11-20');
    expect(Citation.fromJson(cite(1)).sessionDay, '2026-11-21');
    final e = AskEvent.fromJson({
      'type': 'citations',
      'items': [cite(1), 'garbage', {'n': 'x'}, cite(3)],
    }) as AskCitations;
    expect(e.items.map((c) => c.n), [1, 3]);
  });

  test('SSE: an idle byte stream times out', () async {
    final ctl = StreamController<List<int>>();
    final stream = parseAskStream(ctl.stream.timeout(const Duration(milliseconds: 20)));
    await expectLater(stream.toList(), throwsA(isA<TimeoutException>()));
    await ctl.close();
  });

  testWidgets('streaming text, chips, timing, and a new chat', (tester) async {
    final api = await pumpAsk(tester);
    await send(tester, 'Cosa ho promesso?');
    expect(api.asked, ['Cosa ho promesso?']);
    expect(find.text('Cosa ho promesso?'), findsOneWidget);
    expect(find.byKey(const Key('ask-status')), findsOneWidget);

    api.events.add(const AskStatus('waking'));
    await tester.pump();
    expect(find.text(tr('Sveglio il modello…', 'Waking the model…')), findsOneWidget);

    api.events.add(AskCitations([Citation.fromJson(cite(1))]));
    api.events.add(const AskDelta('Hai promesso una '));
    await tester.pump();
    expect(richText('Hai promesso una '), findsOneWidget);
    expect(find.byKey(const Key('ask-status')), findsNothing);
    // Can't send while answering.
    expect(tester.widget<CupertinoTextField>(find.byKey(const Key('ask-input'))).enabled, false);

    api.events.add(const AskDelta('telefonata [1].'));
    api.events.add(const AskDone(timings: {'total_ms': 3400}));
    await tester.pump();
    expect(richText('telefonata'), findsOneWidget);
    expect(find.textContaining('Titolo 1'), findsOneWidget);
    expect(find.byKey(const Key('ask-timing')), findsOneWidget);
    expect(tester.widget<Text>(find.byKey(const Key('ask-timing'))).data, contains(tr('primo testo', 'first text')));
    expect(tester.widget<CupertinoTextField>(find.byKey(const Key('ask-input'))).enabled, true);

    await tester.tap(find.text(tr('Nuova chat', 'New chat')));
    await tester.pump();
    expect(find.text('Cosa ho promesso?'), findsNothing);
  });

  testWidgets('not found is a soft notice; an error has a retry', (tester) async {
    final api = await pumpAsk(tester);
    await send(tester, 'Boh?');
    api.events.add(const AskDelta('Non ho trovato nulla.'));
    api.events.add(const AskDone(notFound: true));
    await tester.pump();
    expect(find.byKey(const Key('ask-not-found')), findsOneWidget);
    expect(find.byKey(const Key('ask-timing')), findsOneWidget);

    await send(tester, 'Ancora?');
    expect(api.asked.length, 2);
    api.events.add(const AskError('Mac fermo'));
    await tester.pump();
    expect(find.text('Mac fermo'), findsOneWidget);
    await tester.tap(find.byKey(const Key('ask-retry')));
    await tester.pump();
    expect(api.asked, ['Boh?', 'Ancora?', 'Ancora?']);
    expect(find.text('Mac fermo'), findsNothing);
  });
}
