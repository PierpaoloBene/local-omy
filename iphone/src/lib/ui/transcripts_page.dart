import 'dart:async';

import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'app_shell.dart';
import 'common.dart';
import 'day_page.dart';

/// "Trascrizioni": the days, newest first, and a search over all of them.
class TranscriptsPage extends StatefulWidget {
  final MacApi api;
  final TabShown shown;
  const TranscriptsPage({super.key, required this.api, required this.shown});

  @override
  State<TranscriptsPage> createState() => _TranscriptsPageState();
}

class _TranscriptsPageState extends State<TranscriptsPage> {
  DayList? _days;
  Object? _error;
  final _query = TextEditingController();
  Timer? _debounce;
  SearchResult? _results;
  String? _searched;
  bool _searching = false;

  @override
  void initState() {
    super.initState();
    widget.shown.addListener(_load);
    _load();
  }

  @override
  void dispose() {
    widget.shown.removeListener(_load);
    _debounce?.cancel();
    _query.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() => _error = null);
    try {
      final d = await widget.api.days();
      if (mounted) setState(() => _days = d);
    } on MacError catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  void _onQuery(String q) {
    _debounce?.cancel();
    if (q.trim().length < 2) {
      setState(() {
        _results = null;
        _searched = null;
      });
      return;
    }
    _debounce = Timer(const Duration(milliseconds: 400), () => _search(q.trim()));
  }

  Future<void> _search(String q) async {
    setState(() => _searching = true);
    try {
      final r = await widget.api.search(q);
      if (mounted && _query.text.trim() == q) {
        setState(() {
          _results = r;
          _searched = q;
        });
      }
    } on MacError catch (e) {
      if (mounted) showMessage(context, e, title: 'Ricerca non riuscita');
    } finally {
      if (mounted) setState(() => _searching = false);
    }
  }

  void _open(String day, {String? focus}) => Navigator.of(context).push(
    CupertinoPageRoute(
      builder: (_) => DayPage(
        api: widget.api,
        day: day,
        focus: focus,
        highlight: focus == null ? null : _searched,
        previousPageTitle: 'Trascrizioni',
      ),
    ),
  );

  @override
  Widget build(BuildContext context) {
    return TabScaffold(
      title: 'Trascrizioni',
      onRefresh: _load,
      busy: _searching,
      searchField: CupertinoSearchTextField(
        controller: _query,
        placeholder: 'Cerca parole o persone',
        onChanged: _onQuery,
        onSubmitted: (q) => q.trim().length >= 2 ? _search(q.trim()) : null,
        onSuffixTap: () {
          _query.clear();
          _onQuery('');
        },
      ),
      slivers: _results != null ? _resultSlivers(context) : _daySlivers(context),
    );
  }

  List<Widget> _resultSlivers(BuildContext context) {
    final r = _results!;
    if (r.results.isEmpty) {
      return [
        SliverFillRemaining(
          hasScrollBody: false,
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(CupertinoIcons.search, size: 44, color: resolve(context, CupertinoColors.tertiaryLabel)),
              const SizedBox(height: 10),
              Text('Nessun risultato per «$_searched»', style: Ios.headline(context)),
            ],
          ),
        ),
      ];
    }
    return [
      SliverToBoxAdapter(
        child: Section(
          header: '${r.results.length}${r.more ? '+' : ''} ${r.results.length == 1 ? 'risultato' : 'risultati'}',
          children: [
            for (final p in r.results)
              IosRow(
                leading: PersonAvatar(name: p.speaker, me: p.isMe, size: 34),
                title: p.speaker ?? 'Voce sconosciuta',
                titleStyle: Ios.headline(context).copyWith(fontSize: 15),
                value: '${shortDay(p.day!)} ${hm(p.start)}',
                chevron: true,
                below: Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text.rich(
                    highlighted(context, p.text, _searched),
                    maxLines: 3,
                    overflow: TextOverflow.ellipsis,
                    style: Ios.subhead(context).copyWith(color: resolve(context, CupertinoColors.secondaryLabel)),
                  ),
                ),
                onTap: () => _open(p.day!, focus: p.id),
              ),
          ],
        ),
      ),
    ];
  }

  List<Widget> _daySlivers(BuildContext context) {
    final d = _days;
    if (d == null) return [LoadState(loading: _error == null, error: _error, onRetry: _load)];
    final p = d.pending;
    return [
      if (p.progress.running || p.sessions > 0)
        SliverToBoxAdapter(
          child: Notice(
            icon: p.progress.running ? CupertinoIcons.hourglass : CupertinoIcons.clock,
            text: p.progress.running
                ? 'Il Mac sta trascrivendo (${p.progress.sessionsDone} di ${p.progress.sessionsTotal} sessioni).'
                : '${p.sessions} ${p.sessions == 1 ? 'sessione' : 'sessioni'} da trascrivere (${duration(p.minutes)}): '
                      'da Oggi puoi farle partire subito.',
          ),
        ),
      if (d.days.isEmpty) const SliverToBoxAdapter(child: Empty('Ancora nessuna trascrizione.')),
      SliverList.separated(
        itemCount: d.days.length,
        separatorBuilder: (_, _) => const SizedBox(height: 12),
        itemBuilder: (context, i) => DaySummaryCard(summary: d.days[i], onTap: () => _open(d.days[i].day)),
      ),
    ];
  }
}
