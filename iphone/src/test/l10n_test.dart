import 'package:flutter_test/flutter_test.dart';
import 'package:local_omy/l10n.dart';
import 'package:local_omy/ui/common.dart';

void main() {
  test('one language at a time, with its plurals and dates', () {
    expect(tr('Oggi', 'Today'), english ? 'Today' : 'Oggi');
    expect(counted(1, 'sessione', 'sessioni', 'session', 'sessions'), english ? '1 session' : '1 sessione');
    expect(counted(3, 'sessione', 'sessioni', 'session', 'sessions'), english ? '3 sessions' : '3 sessioni');
    expect(shortDay('2026-10-29'), english ? 'Oct 29' : '29 ott');
    expect(duration(135), english ? '2.3 h' : '2,3 h');
  });
}
