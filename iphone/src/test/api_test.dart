import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:local_omy/api/mac_api.dart';
import 'package:local_omy/l10n.dart';
import 'package:local_omy/ui/conversations.dart';

import 'fakes.dart';

void main() {
  test('a stopped receiver behind tailscale serve says so', () async {
    final api = await fakeMac((_) async => http.Response('Bad Gateway', 502));
    expect(
      api.places(),
      throwsA(isA<MacError>().having((e) => e.message, 'message', contains(tr('ricevitore', 'receiver')))),
    );
  });

  test('an answer the phone does not understand is an error, not a crash', () async {
    final api = await fakeMac((_) async => json({'places': 'not a list'}));
    expect(
      api.places(),
      throwsA(isA<MacError>().having((e) => e.message, 'message', contains(tr('non valida', 'not valid')))),
    );
  });

  test('today is the Mac\'s', () async {
    final api = await fakeMac(
      (_) async => json({
        'days': [],
        'pending': {
          'sessions': 0,
          'minutes': 0,
          'open_sessions': 0,
          'progress': {'running': false},
        },
        'today': '2026-10-29',
      }),
    );
    await api.days();
    expect(archiveToday, '2026-10-29');
  });

  group('ConversationFeed', () {
    // Three days, a page per day.
    final days = ['2026-10-29', '2026-10-28', '2026-10-27'];
    final requests = <Uri>[];
    var fresh = false;

    Future<http.Response> answer(http.Request r) async {
      requests.add(r.url);
      final before = r.url.queryParameters['before'];
      final i = before == null ? 0 : days.indexOf(before) + 1;
      return json({
        'conversations': [
          conversation(days[i], '${days[i]}-a'),
          if (i == 0 && fresh) conversation(days[i], '${days[i]}-new'),
        ],
        'more': i < days.length - 1,
        'next': days[i],
      });
    }

    test('loads the next pages, then stops', () async {
      requests.clear();
      final feed = ConversationFeed(await fakeMac(answer));
      await feed.refresh();
      expect(feed.list!.conversations.map((c) => c.day), ['2026-10-29']);
      await feed.more();
      await feed.more();
      expect(feed.list!.conversations.map((c) => c.day), days);
      expect(feed.hasMore, isFalse);
      await feed.more();
      expect(requests.length, 3);
      expect(requests[1].queryParameters['before'], '2026-10-29');
    });

    test('a refresh brings the new conversations and keeps the older pages', () async {
      fresh = false;
      final feed = ConversationFeed(await fakeMac(answer));
      await feed.refresh();
      await feed.more();
      fresh = true;
      await feed.refresh();
      expect(feed.list!.conversations.map((c) => c.session), ['2026-10-29-a', '2026-10-29-new', '2026-10-28-a']);
      expect(feed.list!.next, '2026-10-28');
    });

    test('a failed refresh keeps what is on screen, with the error', () async {
      fresh = false;
      var down = false;
      final feed = ConversationFeed(await fakeMac((r) async => down ? http.Response('', 502) : answer(r)));
      await feed.refresh();
      down = true;
      await feed.refresh();
      expect(feed.list!.conversations, hasLength(1));
      expect(feed.error, isA<MacError>());
    });
  });
}
