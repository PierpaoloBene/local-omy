import 'package:flutter/cupertino.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:local_omy/api/models.dart';
import 'package:local_omy/l10n.dart';
import 'package:local_omy/ui/chat.dart';
import 'package:local_omy/ui/timeline.dart';

import 'fakes.dart';

void main() {
  testWidgets('the search word is marked, any case', (tester) async {
    late TextSpan span;
    await tester.pumpWidget(
      CupertinoApp(
        home: Builder(
          builder: (context) {
            span = highlighted(context, 'Ciao Marco, ciao', 'CIAO');
            return const SizedBox();
          },
        ),
      ),
    );
    final marked = span.children!.cast<TextSpan>().where((s) => s.style != null).map((s) => s.text);
    expect(marked, ['Ciao', 'ciao']);
  });

  testWidgets('a short first page asks for the next one', (tester) async {
    final api = await fakeMac((_) async => http.Response('', 404));
    var asked = 0;
    await tester.pumpWidget(
      CupertinoApp(
        home: Timeline(
          api: api,
          title: 'Conversazioni',
          conversations: [Conversation.fromJson(conversation('2026-10-29', 's1'))],
          hasMore: true,
          onMore: () => asked++,
        ),
      ),
    );
    await tester.pump();
    expect(asked, greaterThan(0));
    expect(find.text(tr('Solo tu', 'Just you')), findsOneWidget);
  });
}
