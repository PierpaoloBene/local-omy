import 'dart:async';

import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import '../features.dart';
import '../recording/recorder.dart';
import '../services/ble_service.dart';
import '../settings.dart';
import '../upload/uploader.dart';
import 'app_shell.dart';
import 'common.dart';
import 'chat.dart';
import 'settings_page.dart';

/// "Oggi": the pendant, recording and uploads on the phone; what the Mac
/// has to transcribe; today in short.
class HomePage extends StatefulWidget {
  final BleService ble;
  final Recorder recorder;
  final Uploader uploader;
  final Settings settings;
  final Features features;
  final MacApi api;
  final TabShown shown;

  const HomePage({
    super.key,
    required this.ble,
    required this.recorder,
    required this.uploader,
    required this.settings,
    required this.features,
    required this.api,
    required this.shown,
  });

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> with WidgetsBindingObserver {
  Timer? _tick, _poll;
  DayList? _mac;
  String? _macError;
  bool _starting = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    widget.shown.addListener(_loadMac);
    // The pendant status depends on time (audio quiet, link lost for long).
    _tick = Timer.periodic(const Duration(seconds: 2), (_) => setState(() {}));
    _loadMac();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _loadMac();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    widget.shown.removeListener(_loadMac);
    _tick?.cancel();
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _loadMac() async {
    if (!widget.api.configured) return;
    try {
      final d = await widget.api.days();
      if (!mounted) return;
      setState(() {
        _mac = d;
        _macError = null;
      });
    } on MacError catch (e) {
      if (mounted) setState(() => _macError = e.message);
    }
    _poll?.cancel();
    // Follow a transcription while it runs.
    if (mounted && (_mac?.pending.progress.running ?? false)) {
      _poll = Timer(const Duration(seconds: 5), _loadMac);
    }
  }

  Future<void> _transcribe() async {
    setState(() => _starting = true);
    try {
      await widget.api.transcribeNow();
      await Future.delayed(const Duration(seconds: 2));
      await _loadMac();
    } on MacError catch (e) {
      if (mounted) showMessage(context, e, title: 'Non riuscito');
    } finally {
      if (mounted) setState(() => _starting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return TabScaffold(
      title: 'Oggi',
      onRefresh: () async {
        widget.uploader.trigger();
        await _loadMac();
      },
      trailing: CupertinoButton(
        padding: EdgeInsets.zero,
        child: const Icon(CupertinoIcons.gear),
        onPressed: () async {
          await Navigator.of(context).push(
            CupertinoPageRoute(
              builder: (_) => SettingsPage(
                ble: widget.ble,
                settings: widget.settings,
                uploader: widget.uploader,
                features: widget.features,
              ),
            ),
          );
          _loadMac();
        },
      ),
      slivers: [
        SliverList.list(
          children: [
            _PendantCard(ble: widget.ble, settings: widget.settings),
            const SizedBox(height: 20),
            ListenableBuilder(
              listenable: Listenable.merge([widget.recorder, widget.uploader]),
              builder: (context, _) => _phone(context),
            ),
            _macSection(context),
            ..._today(context),
            ..._errors(context),
          ],
        ),
      ],
    );
  }

  Widget _phone(BuildContext context) {
    final r = widget.recorder, u = widget.uploader;
    return Section(
      header: 'iPhone',
      children: [
        IosRow(
          leading: IconSquare(
            r.recording ? CupertinoIcons.mic_fill : CupertinoIcons.mic_off,
            r.recording ? CupertinoColors.systemRed : CupertinoColors.systemGrey,
          ),
          title: 'Registrazione',
          value: r.recording ? 'Attiva' : 'Ferma',
        ),
        IosRow(
          leading: const IconSquare(CupertinoIcons.tray_arrow_up_fill, CupertinoColors.systemBlue),
          title: 'In coda',
          value: '${u.queued}',
          trailing: u.uploading ? const CupertinoActivityIndicator() : null,
        ),
        IosRow(
          leading: const IconSquare(CupertinoIcons.checkmark_alt, CupertinoColors.systemGreen),
          title: 'Ultimo invio',
          value: u.lastSuccessAt == null ? '—' : _fmt(u.lastSuccessAt!),
        ),
        IosRow(title: 'Invia ora', onTap: u.trigger),
      ],
    );
  }

  Widget _macSection(BuildContext context) {
    final Widget body;
    if (!widget.api.configured) {
      body = const IosRow(title: 'Non configurato', subtitle: 'Imposta URL e token del Mac in Impostazioni.');
    } else if (_macError != null && _mac == null) {
      body = IosRow(
        leading: const IconSquare(CupertinoIcons.wifi_slash, CupertinoColors.systemRed),
        title: 'Non raggiungibile',
        subtitle: _macError,
      );
    } else if (_mac == null) {
      body = const Padding(padding: EdgeInsets.all(14), child: CupertinoActivityIndicator());
    } else {
      final p = _mac!.pending;
      final running = p.progress.running;
      body = IosRow(
        leading: IconSquare(
          running
              ? CupertinoIcons.hourglass
              : p.sessions == 0
              ? CupertinoIcons.checkmark_seal_fill
              : CupertinoIcons.waveform,
          running || p.sessions > 0 ? CupertinoColors.systemPurple : CupertinoColors.systemGreen,
        ),
        title: running
            ? 'Trascrizione in corso'
            : p.sessions == 0
            ? 'Tutto trascritto'
            : '${p.sessions} ${p.sessions == 1 ? 'sessione' : 'sessioni'} da trascrivere',
        subtitle: [
          if (running) '${p.progress.sessionsDone} di ${p.progress.sessionsTotal} sessioni',
          if (!running && p.sessions > 0) duration(p.minutes),
          if (p.openSessions > 0)
            '${p.openSessions == 1 ? 'una sessione ancora aperta' : '${p.openSessions} sessioni ancora aperte'}, '
                'si trascrive quando finisce',
        ].join(' · ').emptyToNull,
        below: running
            ? Padding(padding: const EdgeInsets.only(top: 8), child: ProgressBar(p.progress.fraction))
            : null,
      );
    }
    final p = _mac?.pending;
    return Section(
      header: 'Mac',
      children: [
        body,
        if (p != null && !p.progress.running && p.sessions > 0)
          IosRow(
            title: 'Trascrivi ora',
            onTap: _starting ? null : _transcribe,
            trailing: _starting ? const CupertinoActivityIndicator() : null,
          ),
      ],
    );
  }

  List<Widget> _today(BuildContext context) {
    final today = _mac?.days.where((d) => d.day == todayIso()).firstOrNull;
    if (today == null) return [];
    return [
      Padding(
        padding: const EdgeInsets.fromLTRB(32, 16, 32, 6),
        child: Text('OGGI IN BREVE', style: Ios.footnote(context)),
      ),
      DaySummaryCard(summary: today, onTap: _openToday),
    ];
  }

  /// Today's conversations, from the latest.
  Future<void> _openToday() async {
    try {
      final l = await widget.api.conversations(day: todayIso(), limit: 1000);
      if (!mounted || l.conversations.isEmpty) return;
      await ConversationViewer.open(context, widget.api, l.conversations, 0, previousPageTitle: 'Oggi');
    } on MacError catch (e) {
      if (mounted) showMessage(context, e, title: 'Non riuscito');
    }
  }

  List<Widget> _errors(BuildContext context) {
    final r = widget.recorder, u = widget.uploader;
    final candidates = [
      if (r.lastErrorAt != null) (r.lastErrorAt!, r.lastError!),
      if (u.lastErrorAt != null) (u.lastErrorAt!, u.lastError!),
    ]..sort((a, b) => b.$1.compareTo(a.$1));
    final e = candidates.firstOrNull;
    if (e == null) return [];
    return [
      const SizedBox(height: 20),
      Section(
        header: 'Ultimo errore',
        children: [
          IosRow(
            leading: const IconSquare(CupertinoIcons.exclamationmark_triangle_fill, CupertinoColors.systemRed),
            title: _fmt(e.$1),
            subtitle: e.$2,
          ),
        ],
      ),
    ];
  }
}

extension on String {
  String? get emptyToNull => isEmpty ? null : this;
}

String _fmt(DateTime t) {
  String two(int v) => v.toString().padLeft(2, '0');
  final day = t.toIso8601String().substring(0, 10);
  return '${day == todayIso() ? '' : '${shortDay(day)} '}${two(t.hour)}:${two(t.minute)}';
}

class _PendantCard extends StatelessWidget {
  final BleService ble;
  final Settings settings;
  const _PendantCard({required this.ble, required this.settings});

  @override
  Widget build(BuildContext context) {
    final status = ble.status;
    final (Color color, IconData icon, String title, String detail) = switch (status) {
      PendantStatus.connected => (CupertinoColors.systemGreen, CupertinoIcons.waveform, 'Connesso', 'Sta registrando'),
      PendantStatus.idle => (
        CupertinoColors.systemYellow,
        CupertinoIcons.pause_fill,
        'Inattivo',
        'Collegato, ma non arriva audio',
      ),
      PendantStatus.disconnected => (
        CupertinoColors.systemOrange,
        CupertinoIcons.antenna_radiowaves_left_right,
        'Disconnesso',
        'Si ricollega da solo${ble.lastConnectedAt == null ? '' : ' · perso alle ${_fmt(ble.lastConnectedAt!)}'}',
      ),
      PendantStatus.notFound => (
        CupertinoColors.systemGrey,
        CupertinoIcons.bluetooth,
        'Non trovato',
        settings.deviceId == null
            ? 'Nessun ciondolo scelto: cercalo in Impostazioni'
            : !ble.bluetoothOn
            ? 'Bluetooth spento'
            : 'Lontano o scarico${ble.lastConnectedAt == null ? '' : ' · visto alle ${_fmt(ble.lastConnectedAt!)}'}',
      ),
    };
    final c = resolve(context, color);
    final live = status == PendantStatus.connected || status == PendantStatus.idle;
    return InsetCard(
      padding: const EdgeInsets.all(18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 54,
                height: 54,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  gradient: LinearGradient(
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                    colors: [Color.lerp(c, const Color(0xFFFFFFFF), 0.2)!, c],
                  ),
                ),
                child: Icon(icon, color: const Color(0xFFFFFFFF), size: 26),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Ciondolo', style: Ios.footnote(context)),
                    Text(title, style: Ios.title(context)),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Text(detail, style: Ios.subhead(context)),
          if (ble.battery != null || ble.firmware != null) ...[
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (ble.battery != null)
                  Pill(
                    '${ble.battery}%${live ? '' : ' (ultimo)'}',
                    leading: Icon(_batteryIcon(ble.battery!), size: 20, color: _batteryColor(context, ble.battery!)),
                  ),
                if (ble.firmware != null) Pill('Firmware ${ble.firmware}'),
              ],
            ),
          ],
        ],
      ),
    );
  }

  static IconData _batteryIcon(int level) => switch (level) {
    >= 75 => CupertinoIcons.battery_full,
    >= 40 => CupertinoIcons.battery_75_percent,
    >= 15 => CupertinoIcons.battery_25_percent,
    _ => CupertinoIcons.battery_empty,
  };

  static Color _batteryColor(BuildContext context, int level) =>
      resolve(context, level < 20 ? CupertinoColors.systemRed : CupertinoColors.systemGreen);
}
