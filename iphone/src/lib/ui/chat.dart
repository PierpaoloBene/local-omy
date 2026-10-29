import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'common.dart';
import '../l10n.dart';

/// Conversations like Messages threads, one per page: you on the right,
/// the others on the left. Swipe right for the one before, left for the one
/// after. With [focus] (a paragraph id, from a search) the first page opens
/// there, with [highlight] marked.
class ConversationViewer extends StatefulWidget {
  final MacApi api;

  /// Newest first, as in the timeline.
  final List<Conversation> conversations;
  final int initial;
  final String? focus, highlight;
  final String previousPageTitle;
  const ConversationViewer({
    super.key,
    required this.api,
    required this.conversations,
    this.initial = 0,
    this.focus,
    this.highlight,
    required this.previousPageTitle,
  });

  static Future<void> open(
    BuildContext context,
    MacApi api,
    List<Conversation> conversations,
    int initial, {
    required String previousPageTitle,
    String? focus,
    String? highlight,
  }) => Navigator.of(context).push(
    CupertinoPageRoute(
      builder: (_) => ConversationViewer(
        api: api,
        conversations: conversations,
        initial: initial,
        focus: focus,
        highlight: highlight,
        previousPageTitle: previousPageTitle,
      ),
    ),
  );

  @override
  State<ConversationViewer> createState() => _ConversationViewerState();
}

class _ConversationViewerState extends State<ConversationViewer> {
  late final _pages = PageController(initialPage: widget.initial);
  late int _index = widget.initial;
  final _days = <String, Future<Day>>{};

  Future<Day> _day(String day) => _days.putIfAbsent(day, () => widget.api.day(day));

  @override
  void dispose() {
    _pages.dispose();
    super.dispose();
  }

  void _go(int i) => _pages.animateToPage(i, duration: const Duration(milliseconds: 300), curve: Curves.easeOut);

  @override
  Widget build(BuildContext context) {
    final c = widget.conversations[_index];
    return CupertinoPageScaffold(
      backgroundColor: CupertinoColors.systemGroupedBackground,
      navigationBar: CupertinoNavigationBar(
        previousPageTitle: widget.previousPageTitle,
        middle: FittedBox(
          fit: BoxFit.scaleDown,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text('${longDay(c.day)} ${hm(c.start)}', style: Ios.headline(context).copyWith(fontSize: 16)),
              if (c.app != null || c.place != null) Text(c.app ?? c.place!, style: Ios.caption(context), maxLines: 1),
            ],
          ),
        ),
        trailing: widget.conversations.length < 2
            ? null
            : Text(
                '${widget.conversations.length - _index}/${widget.conversations.length}',
                style: Ios.footnote(context),
              ),
      ),
      child: PageView.builder(
        controller: _pages,
        // Newest first: swiping right goes back in time, like Photos.
        reverse: true,
        itemCount: widget.conversations.length,
        onPageChanged: (i) => setState(() => _index = i),
        itemBuilder: (context, i) => _Thread(
          key: ValueKey(widget.conversations[i].session),
          conversation: widget.conversations[i],
          day: _day(widget.conversations[i].day),
          focus: i == widget.initial ? widget.focus : null,
          highlight: widget.highlight,
          earlier: i + 1 < widget.conversations.length ? widget.conversations[i + 1] : null,
          later: i > 0 ? widget.conversations[i - 1] : null,
          onEarlier: () => _go(i + 1),
          onLater: () => _go(i - 1),
        ),
      ),
    );
  }
}

/// One conversation: who was there, then the bubbles; at the end, the way
/// to the one before and the one after.
class _Thread extends StatefulWidget {
  final Conversation conversation;
  final Future<Day> day;
  final String? focus, highlight;
  final Conversation? earlier, later;
  final VoidCallback onEarlier, onLater;
  const _Thread({
    super.key,
    required this.conversation,
    required this.day,
    this.focus,
    this.highlight,
    this.earlier,
    this.later,
    required this.onEarlier,
    required this.onLater,
  });

  @override
  State<_Thread> createState() => _ThreadState();
}

class _ThreadState extends State<_Thread> with AutomaticKeepAliveClientMixin {
  final _center = UniqueKey();
  final _scroll = ScrollController();

  @override
  bool get wantKeepAlive => true;

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    return FutureBuilder<Day>(
      future: widget.day,
      builder: (context, snap) {
        if (snap.hasError) {
          return Center(
            child: Text(
              tr('Il Mac non ha risposto: torna indietro e riprova.', 'The Mac did not answer: go back and try again.'),
              style: Ios.subhead(context),
            ),
          );
        }
        if (!snap.hasData) return const Center(child: CupertinoActivityIndicator());
        final paragraphs = [
          for (final p in snap.data!.paragraphs)
            if (p.session == widget.conversation.session) p,
        ];
        final (bubbles, focused) = _bubbles(paragraphs);
        final items = [
          const SizedBox(height: 12),
          _Who(conversation: widget.conversation),
          ...bubbles,
          const SizedBox(height: 24),
          _Neighbours(
            earlier: widget.earlier,
            later: widget.later,
            onEarlier: widget.onEarlier,
            onLater: widget.onLater,
          ),
          SizedBox(height: MediaQuery.paddingOf(context).bottom + 24),
        ];
        // Built lazily, a long meeting too. The focused paragraph (from a
        // search) is the center: the list opens there, the rest grows up
        // and down from it.
        final at = focused == null ? 0 : focused + 2;
        return CupertinoScrollbar(
          controller: _scroll,
          thumbVisibility: true,
          thickness: 6,
          thicknessWhileDragging: 10,
          child: CustomScrollView(
            controller: _scroll,
            center: _center,
            anchor: focused == null ? 0 : 0.3,
            slivers: [
              SliverList(delegate: SliverChildBuilderDelegate((context, i) => items[at - 1 - i], childCount: at)),
              SliverList(
                key: _center,
                delegate: SliverChildBuilderDelegate((context, i) => items[at + i], childCount: items.length - at),
              ),
            ],
          ),
        );
      },
    );
  }

  /// The bubbles, with a line where the place changes; and the index of
  /// the focused one.
  (List<Widget>, int?) _bubbles(List<Paragraph> paragraphs) {
    final out = <Widget>[];
    int? focusedAt;
    String? place, speaker;
    for (final p in paragraphs) {
      final label = p.place?.label;
      if (label != null && label != place && place != null) {
        out.add(_SessionHeader(time: hm(p.start), place: label, named: p.place?.name != null));
        speaker = null;
      }
      place = label ?? place;
      final focused = p.id == widget.focus;
      if (focused) focusedAt = out.length;
      out.add(
        Bubble(
          paragraph: p,
          highlight: widget.highlight,
          focused: focused,
          first: p.speaker != speaker || p.speaker == null,
        ),
      );
      speaker = p.speaker;
    }
    if (out.isEmpty)
      out.add(
        Empty(
          tr(
            'Nessun paragrafo: forse la conversazione è stata trascritta di nuovo.',
            'No paragraphs: maybe the conversation was transcribed again.',
          ),
        ),
      );
    return (out, focusedAt);
  }
}

/// Who spoke, and for how long.
class _Who extends StatelessWidget {
  final Conversation conversation;
  const _Who({required this.conversation});

  @override
  Widget build(BuildContext context) {
    final c = conversation;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Column(
        children: [
          Text(
            [
              '${hm(c.start)}–${hm(c.end)}',
              duration(c.minutes),
              if (c.place != null && c.app != null) c.place!,
            ].join(' · '),
            style: Ios.caption(context),
          ),
          if (c.participants.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              alignment: WrapAlignment.center,
              children: [
                for (final p in c.participants)
                  Pill(
                    '${p.name} · ${duration(p.minutes)}',
                    leading: PersonAvatar(name: p.name, me: p.isMe, size: 22),
                  ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}

class _Neighbours extends StatelessWidget {
  final Conversation? earlier, later;
  final VoidCallback onEarlier, onLater;
  const _Neighbours({this.earlier, this.later, required this.onEarlier, required this.onLater});

  @override
  Widget build(BuildContext context) {
    Widget side(Conversation? c, bool before) => Expanded(
      child: c == null
          ? const SizedBox()
          : CupertinoButton(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              onPressed: before ? onEarlier : onLater,
              child: Column(
                crossAxisAlignment: before ? CrossAxisAlignment.start : CrossAxisAlignment.end,
                children: [
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if (before) const Icon(CupertinoIcons.chevron_back, size: 16),
                      Text(before ? tr('Prima', 'Earlier') : tr('Dopo', 'Later')),
                      if (!before) const Icon(CupertinoIcons.chevron_forward, size: 16),
                    ],
                  ),
                  Text('${longDay(c.day)} ${hm(c.start)}', style: Ios.caption(context)),
                ],
              ),
            ),
    );
    return Row(children: [side(earlier, true), side(later, false)]);
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
              p.speaker ?? tr('Voce sconosciuta', 'Unknown voice'),
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
  // A few letters change length in lower case: their offsets wouldn't match.
  if (lower.length != s.length) return TextSpan(text: s);
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
                        counted(s.sessions, 'sessione', 'sessioni', 'session', 'sessions'),
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
                      [
                        ...speakers.map((sp) => sp.name),
                        if (hidden > 0) tr('altri $hidden', '$hidden more'),
                      ].join(', '),
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
                      if (s.unnamedPlaces)
                        s.places.isEmpty
                            ? tr('luoghi senza nome', 'unnamed places')
                            : tr('altri senza nome', 'others without a name'),
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
