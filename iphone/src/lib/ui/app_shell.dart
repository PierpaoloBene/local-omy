import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../features.dart';
import '../recording/recorder.dart';
import '../services/ble_service.dart';
import '../settings.dart';
import '../upload/uploader.dart';
import 'home_page.dart';
import 'people_page.dart';
import 'places_page.dart';
import 'transcripts_page.dart';

/// Fires when its tab is selected again, so the page reloads from the Mac.
class TabShown extends ChangeNotifier {
  void fire() => notifyListeners();
}

/// The four tabs, each with its own navigation stack, as on iOS.
class AppShell extends StatefulWidget {
  final BleService ble;
  final Recorder recorder;
  final Uploader uploader;
  final Settings settings;
  final Features features;
  final MacApi api;

  const AppShell({
    super.key,
    required this.ble,
    required this.recorder,
    required this.uploader,
    required this.settings,
    required this.features,
    required this.api,
  });

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  final _controller = CupertinoTabController();
  final _shown = List.generate(4, (_) => TabShown());
  int _last = 0;

  @override
  void initState() {
    super.initState();
    _controller.addListener(() {
      if (_controller.index != _last) _shown[_controller.index].fire();
      _last = _controller.index;
    });
  }

  @override
  void dispose() {
    _controller.dispose();
    for (final s in _shown) {
      s.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return CupertinoTabScaffold(
      controller: _controller,
      tabBar: CupertinoTabBar(
        items: const [
          BottomNavigationBarItem(icon: Icon(CupertinoIcons.dot_radiowaves_left_right), label: 'Oggi'),
          BottomNavigationBarItem(
            icon: Icon(CupertinoIcons.chat_bubble_2),
            activeIcon: Icon(CupertinoIcons.chat_bubble_2_fill),
            label: 'Conversazioni',
          ),
          BottomNavigationBarItem(
            icon: Icon(CupertinoIcons.person_2),
            activeIcon: Icon(CupertinoIcons.person_2_fill),
            label: 'Persone',
          ),
          BottomNavigationBarItem(
            icon: Icon(CupertinoIcons.map_pin),
            activeIcon: Icon(CupertinoIcons.map_pin_ellipse),
            label: 'Luoghi',
          ),
        ],
      ),
      tabBuilder: (context, i) => CupertinoTabView(
        builder: (context) => switch (i) {
          0 => HomePage(
            ble: widget.ble,
            recorder: widget.recorder,
            uploader: widget.uploader,
            settings: widget.settings,
            features: widget.features,
            api: widget.api,
            shown: _shown[0],
          ),
          1 => TranscriptsPage(api: widget.api, shown: _shown[1]),
          2 => PeoplePage(api: widget.api, features: widget.features, shown: _shown[2]),
          _ => PlacesPage(api: widget.api, features: widget.features, shown: _shown[3]),
        },
      ),
    );
  }
}
