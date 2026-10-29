/// Italian or English, after the iPhone's language: Italian if it comes
/// before English in Settings → General → Language & Region, else English.
/// iOS restarts the app when the language changes, so it is read once.
library;

import 'dart:ui';

final bool english = () {
  for (final l in PlatformDispatcher.instance.locales) {
    if (l.languageCode == 'it') return false;
    if (l.languageCode == 'en') return true;
  }
  return true;
}();

/// The text in the app's language, the Italian first.
String tr(String it, String en) => english ? en : it;

/// "1 sessione", "3 sessioni"; "1 session", "3 sessions".
String counted(int n, String itOne, String itMany, String enOne, String enMany) =>
    '$n ${n == 1 ? tr(itOne, enOne) : tr(itMany, enMany)}';
