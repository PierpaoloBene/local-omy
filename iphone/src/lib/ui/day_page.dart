import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'common.dart';

/// One day like a Messages thread: you on the right, the others on the
/// left, a time and place where a session starts or moves. With [focus] (a
/// paragraph id, from a search) it scrolls there and highlights [highlight].
class DayPage extends StatefulWidget {
  final MacApi api;
  final String day;
  final String? focus;
  final String? highlight;
  final String? previousPageTitle;
  const DayPage({super.key, required this.api, required this.day, this.focus, this.highlight, this.previousPageTitle});

  @override
  State<DayPage> createState() => _DayPageState();
}

class _DayPageState extends State<DayPage> {
  Day? _day;
  Object? _error;
  final _focusKey = GlobalKey();

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _error = null);
    try {
      final d = await widget.api.day(widget.day);
      if (!mounted) return;
      setState(() => _day = d);
      if (widget.focus != null) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          final c = _focusKey.currentContext;
          if (c != null) Scrollable.ensureVisible(c, alignment: 0.35, duration: const Duration(milliseconds: 350));
        });
      }
    } on MacError catch (e) {
      if (mounted) setState(() => _error = e);
    }
  }

  @override
  Widget build(BuildContext context) {
    final d = _day;
    return TabScaffold(
      title: longDay(widget.day),
      previousPageTitle: widget.previousPageTitle ?? 'Trascrizioni',
      onRefresh: _load,
      slivers: d == null
          ? [LoadState(loading: _error == null, error: _error, onRetry: _load)]
          : [
              SliverToBoxAdapter(child: DaySummaryCard(summary: d.summary, detailed: true)),
              // Not lazy, so a searched paragraph can be scrolled to.
              SliverToBoxAdapter(child: Column(children: _thread(context, d))),
            ],
    );
  }

  List<Widget> _thread(BuildContext context, Day d) {
    final out = <Widget>[];
    String? session, place, speaker;
    for (final p in d.paragraphs) {
      final label = p.place?.label;
      if (p.session != session || (label != null && label != place)) {
        out.add(_SessionHeader(time: hm(p.start), place: label, named: p.place?.name != null));
        session = p.session;
        place = label;
        speaker = null;
      }
      final focused = p.id == widget.focus;
      out.add(
        Bubble(
          key: focused ? _focusKey : null,
          paragraph: p,
          highlight: widget.highlight,
          focused: focused,
          first: p.speaker != speaker || p.speaker == null,
        ),
      );
      speaker = p.speaker;
    }
    if (out.isEmpty)
      out.add(const Padding(padding: EdgeInsets.all(32), child: Empty('Nessun paragrafo in questo giorno.')));
    return out;
  }
}

class _SessionHeader extends StatelessWidget {
  final String time;
  final String? place;
  final bool named;
  const _SessionHeader({required this.time, required this.place, required this.named});

  @override
  Widget build(BuildContext context) {
    final style = Ios.caption(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 22, 20, 6),
      child: Text.rich(
        TextSpan(
          children: [
            TextSpan(
              text: time,
              style: style.copyWith(fontWeight: FontWeight.w600),
            ),
            if (place != null) ...[
              const TextSpan(text: '  '),
              WidgetSpan(
                alignment: PlaceholderAlignment.middle,
                child: Icon(
                  named ? CupertinoIcons.location_solid : CupertinoIcons.location,
                  size: 11,
                  color: style.color,
                ),
              ),
              TextSpan(text: ' $place'),
            ],
          ],
        ),
        style: style,
        textAlign: TextAlign.center,
      ),
    );
  }
}

/// A paragraph as a message bubble.
class Bubble extends StatelessWidget {
  final Paragraph paragraph;
  final String? highlight;
  final bool focused;

  /// First of a run by the same speaker: name and avatar.
  final bool first;

  const Bubble({super.key, required this.paragraph, this.highlight, this.focused = false, this.first = true});

  @override
  Widget build(BuildContext context) {
    final p = paragraph;
    final me = p.isMe;
    final fg = me ? const Color(0xFFFFFFFF) : resolve(context, CupertinoColors.label);
    final bg = me ? resolve(context, tint) : resolve(context, CupertinoColors.secondarySystemGroupedBackground);
    final bubble = Container(
      padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 8),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(18),
        border: focused ? Border.all(color: resolve(context, CupertinoColors.systemYellow), width: 2.5) : null,
      ),
      child: Text.rich(
        highlighted(context, p.text, highlight),
        style: Ios.body(context).copyWith(color: fg, fontSize: 16, height: 1.3),
      ),
    );
    final meta = Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (!me) ...[
          Flexible(
            child: Text(
              p.speaker ?? 'Voce sconosciuta',
              overflow: TextOverflow.ellipsis,
              style: Ios.caption(context).copyWith(color: personColor(context, p.speaker), fontWeight: FontWeight.w600),
            ),
          ),
          const SizedBox(width: 6),
        ],
        if (p.via != null) ...[
          Icon(
            p.via!.kind == 'call'
                ? (p.via!.app == 'iPhone' ? CupertinoIcons.phone_fill : CupertinoIcons.videocam_fill)
                : p.via!.app == 'TV'
                ? CupertinoIcons.tv_fill
                : CupertinoIcons.play_rectangle_fill,
            size: 11,
            color: Ios.caption(context).color,
          ),
          const SizedBox(width: 3),
          if (p.via!.app != null) Text('${p.via!.app}  ', style: Ios.caption(context)),
        ],
        Text(hm(p.start), style: Ios.caption(context)),
      ],
    );
    return Padding(
      padding: EdgeInsets.fromLTRB(me ? 56 : 10, first ? 8 : 2, me ? 12 : 48, 0),
      child: Row(
        mainAxisAlignment: me ? MainAxisAlignment.end : MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          if (!me) ...[
            SizedBox(width: 30, child: first ? PersonAvatar(name: p.speaker, size: 30) : null),
            const SizedBox(width: 6),
          ],
          Flexible(
            child: Column(
              crossAxisAlignment: me ? CrossAxisAlignment.end : CrossAxisAlignment.start,
              children: [
                if (first) Padding(padding: const EdgeInsets.only(left: 12, right: 12, bottom: 3), child: meta),
                bubble,
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// [s] with every occurrence of [q] (any case) marked.
TextSpan highlighted(BuildContext context, String s, String? q) {
  if (q == null || q.trim().isEmpty) return TextSpan(text: s);
  final needle = q.trim().toLowerCase();
  final lower = s.toLowerCase();
  final mark = TextStyle(
    backgroundColor: resolve(context, CupertinoColors.systemYellow).withValues(alpha: 0.85),
    color: const Color(0xFF000000),
  );
  final spans = <TextSpan>[];
  var i = 0;
  while (true) {
    final j = lower.indexOf(needle, i);
    if (j < 0) break;
    spans.add(TextSpan(text: s.substring(i, j)));
    spans.add(TextSpan(text: s.substring(j, j + needle.length), style: mark));
    i = j + needle.length;
  }
  spans.add(TextSpan(text: s.substring(i)));
  return TextSpan(children: spans);
}

/// A day in short: when, how long, who and where. In the list of days, and
/// (detailed) at the top of the day.
class DaySummaryCard extends StatelessWidget {
  final DaySummary summary;
  final bool detailed;
  final VoidCallback? onTap;
  const DaySummaryCard({super.key, required this.summary, this.detailed = false, this.onTap});

  @override
  Widget build(BuildContext context) {
    final s = summary;
    final speakers = detailed ? s.speakers : s.speakers.take(4).toList();
    final hidden = s.speakers.length - speakers.length;
    final secondary = resolve(context, CupertinoColors.secondaryLabel);
    return InsetCard(
      onTap: onTap,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (!detailed) Text(longDay(s.day), style: Ios.headline(context)),
                    Text(
                      [
                        if (s.from != null) '${s.from}–${s.to}',
                        duration(s.minutes),
                        '${s.sessions} ${s.sessions == 1 ? 'sessione' : 'sessioni'}',
                      ].join(' · '),
                      style: Ios.subhead(context).copyWith(color: secondary),
                    ),
                  ],
                ),
              ),
              if (onTap != null)
                Icon(CupertinoIcons.chevron_forward, size: 17, color: resolve(context, CupertinoColors.tertiaryLabel)),
            ],
          ),
          if (speakers.isNotEmpty) ...[
            const SizedBox(height: 12),
            if (detailed)
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: [
                  for (final sp in speakers)
                    Pill(
                      '${sp.name} · ${duration(sp.minutes)}',
                      leading: PersonAvatar(name: sp.name, me: sp.isMe, size: 22),
                    ),
                ],
              )
            else
              Row(
                children: [
                  // Overlapping avatars, like a group in Messages.
                  SizedBox(
                    width: 26.0 * speakers.length + 6,
                    height: 32,
                    child: Stack(
                      children: [
                        for (final (i, sp) in speakers.indexed)
                          Positioned(
                            left: 26.0 * i,
                            child: Container(
                              padding: const EdgeInsets.all(1.5),
                              decoration: BoxDecoration(
                                shape: BoxShape.circle,
                                color: resolve(context, CupertinoColors.secondarySystemGroupedBackground),
                              ),
                              child: PersonAvatar(name: sp.name, me: sp.isMe, size: 29),
                            ),
                          ),
                      ],
                    ),
                  ),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      [...speakers.map((sp) => sp.name), if (hidden > 0) 'altri $hidden'].join(', '),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: Ios.footnote(context),
                    ),
                  ),
                ],
              ),
          ],
          if (s.places.isNotEmpty || s.unnamedPlaces) ...[
            const SizedBox(height: 10),
            Row(
              children: [
                Icon(CupertinoIcons.location_solid, size: 13, color: secondary),
                const SizedBox(width: 4),
                Expanded(
                  child: Text(
                    [
                      ...s.places,
                      if (s.unnamedPlaces) s.places.isEmpty ? 'luoghi senza nome' : 'altri senza nome',
                    ].join(' · '),
                    style: Ios.footnote(context),
                  ),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}
