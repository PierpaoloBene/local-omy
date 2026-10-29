import 'package:flutter/cupertino.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'chat.dart';
import 'common.dart';
import 'conversations.dart';

/// Every conversation, newest first, in one scroll: the day stays at the top
/// while you scroll through it, and the index on the right edge jumps to a
/// day, like the letters in Contacts. Rows and day headers have a fixed
/// height, so where a day starts is known before it is built.
class Timeline extends StatefulWidget {
  final MacApi api;
  final String title;
  final String? previousPageTitle;
  final Widget? searchField;
  final Widget? trailing;
  final bool busy;
  final Future<void> Function()? onRefresh;

  /// Above the conversations (filters, a person's card).
  final List<Widget> top;

  /// null while loading.
  final List<Conversation>? conversations;
  final Object? error;
  final VoidCallback? onRetry;
  final String empty;

  /// Shown instead of the conversations (search results).
  final List<Widget>? replacement;

  const Timeline({
    super.key,
    required this.api,
    required this.title,
    this.previousPageTitle,
    this.searchField,
    this.trailing,
    this.busy = false,
    this.onRefresh,
    this.top = const [],
    required this.conversations,
    this.error,
    this.onRetry,
    this.empty = 'Nessuna conversazione.',
    this.replacement,
  });

  @override
  State<Timeline> createState() => _TimelineState();
}

const _headerExtent = 40.0;

class _TimelineState extends State<Timeline> {
  final _scroll = ScrollController();
  final _start = GlobalKey();
  String? _scrubbing;

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  static List<(String, List<Conversation>)> _byDay(List<Conversation> cs) {
    final out = <(String, List<Conversation>)>[];
    for (final c in cs) {
      if (out.isEmpty || out.last.$1 != c.day) out.add((c.day, []));
      out.last.$2.add(c);
    }
    return out;
  }

  double _rowExtent(BuildContext context) => 30 + 58 * MediaQuery.textScalerOf(context).scale(1);

  void _jump(List<(String, List<Conversation>)> days, int i) {
    final start = _start.currentContext?.findRenderObject();
    if (start is! RenderSliver || !_scroll.hasClients) return;
    var y = start.constraints.precedingScrollExtent;
    final row = _rowExtent(context);
    for (final (_, cs) in days.take(i)) {
      y += _headerExtent + cs.length * row;
    }
    // Under the collapsed navigation bar.
    final bar = MediaQuery.paddingOf(context).top + 44;
    _scroll.jumpTo((y - bar + 1).clamp(0, _scroll.position.maxScrollExtent));
  }

  void _open(List<Conversation> all, Conversation c) => ConversationViewer.open(
    context,
    widget.api,
    all,
    all.indexOf(c),
    previousPageTitle: widget.title.length > 14 ? 'Indietro' : widget.title,
  );

  @override
  Widget build(BuildContext context) {
    final all = widget.conversations;
    final days = all == null ? <(String, List<Conversation>)>[] : _byDay(all);
    final row = _rowExtent(context);
    final right = widget.busy ? const CupertinoActivityIndicator() : widget.trailing;
    final pad = MediaQuery.paddingOf(context);
    final scrubber = widget.replacement == null && days.length > 1;
    return CupertinoPageScaffold(
      backgroundColor: CupertinoColors.systemGroupedBackground,
      child: Stack(
        children: [
          CupertinoScrollbar(
            controller: _scroll,
            child: CustomScrollView(
              controller: _scroll,
              physics: const AlwaysScrollableScrollPhysics(parent: BouncingScrollPhysics()),
              slivers: [
                if (widget.searchField != null)
                  CupertinoSliverNavigationBar.search(
                    largeTitle: Text(widget.title),
                    previousPageTitle: widget.previousPageTitle,
                    trailing: right,
                    searchField: widget.searchField!,
                  )
                else
                  CupertinoSliverNavigationBar(
                    largeTitle: Text(widget.title),
                    previousPageTitle: widget.previousPageTitle,
                    trailing: right,
                  ),
                if (widget.onRefresh != null) CupertinoSliverRefreshControl(onRefresh: widget.onRefresh),
                const SliverToBoxAdapter(child: SizedBox(height: 8)),
                ...widget.top,
                if (widget.replacement != null)
                  ...widget.replacement!
                else if (all == null)
                  LoadState(loading: widget.error == null, error: widget.error, onRetry: widget.onRetry ?? () {})
                else ...[
                  SliverToBoxAdapter(key: _start, child: const SizedBox.shrink()),
                  if (all.isEmpty) SliverToBoxAdapter(child: Empty(widget.empty)),
                  for (final (day, cs) in days)
                    SliverMainAxisGroup(
                      slivers: [
                        SliverPersistentHeader(pinned: true, delegate: _DayHeader(day, cs)),
                        SliverFixedExtentList(
                          itemExtent: row,
                          delegate: SliverChildBuilderDelegate(
                            (context, i) => TimelineRow(c: cs[i], onTap: () => _open(all, cs[i])),
                            childCount: cs.length,
                          ),
                        ),
                      ],
                    ),
                ],
                SliverToBoxAdapter(child: SizedBox(height: 40 + pad.bottom)),
              ],
            ),
          ),
          if (scrubber)
            Positioned(
              right: 0,
              top: pad.top + 120,
              bottom: pad.bottom + 16,
              child: _DayIndex(
                days: [for (final (d, _) in days) d],
                onDay: (i) => _jump(days, i),
                onScrub: (d) => setState(() => _scrubbing = d),
              ),
            ),
          if (_scrubbing != null)
            Center(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 14),
                decoration: BoxDecoration(
                  color: resolve(context, CupertinoColors.systemGrey).withValues(alpha: 0.9),
                  borderRadius: BorderRadius.circular(16),
                ),
                child: Text(longDay(_scrubbing!), style: Ios.title(context).copyWith(color: const Color(0xFFFFFFFF))),
              ),
            ),
        ],
      ),
    );
  }
}

/// The day, pinned while its conversations scroll by.
class _DayHeader extends SliverPersistentHeaderDelegate {
  final String day;
  final List<Conversation> conversations;
  _DayHeader(this.day, this.conversations);

  @override
  double get minExtent => _headerExtent;
  @override
  double get maxExtent => _headerExtent;

  @override
  bool shouldRebuild(_DayHeader old) => old.day != day || old.conversations != conversations;

  @override
  Widget build(BuildContext context, double shrinkOffset, bool overlapsContent) {
    final minutes = conversations.fold(0, (n, c) => n + c.minutes);
    final n = conversations.length;
    return Container(
      color: resolve(context, CupertinoColors.systemGroupedBackground).withValues(alpha: 0.96),
      padding: const EdgeInsets.fromLTRB(20, 0, 36, 0),
      alignment: Alignment.centerLeft,
      child: Row(
        children: [
          Text(longDay(day), style: Ios.headline(context)),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '$n ${n == 1 ? 'conversazione' : 'conversazioni'} · ${duration(minutes)}',
              style: Ios.footnote(context),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
        ],
      ),
    );
  }
}

/// A conversation in the timeline, always the same height.
class TimelineRow extends StatelessWidget {
  final Conversation c;
  final VoidCallback onTap;
  const TimelineRow({super.key, required this.c, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final others = c.participants.where((p) => !p.isMe).toList();
    final type = c.types.contains('media')
        ? 'media'
        : c.types.contains('call')
        ? 'call'
        : c.types.contains('meeting')
        ? 'meeting'
        : 'room';
    final faces = (others.isEmpty ? c.participants : others).take(3).toList();
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 4, 36, 4),
      child: CupertinoButton(
        padding: EdgeInsets.zero,
        pressedOpacity: 0.7,
        onPressed: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            color: resolve(context, CupertinoColors.secondarySystemGroupedBackground),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Row(
            children: [
              SizedBox(
                width: 40,
                height: 40,
                child: Stack(
                  children: [
                    for (final (i, p) in faces.indexed)
                      Positioned(
                        left: faces.length == 1 ? 2 : i * 8.0,
                        top: faces.length == 1 ? 2 : i * 6.0,
                        child: Container(
                          padding: const EdgeInsets.all(1),
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            color: resolve(context, CupertinoColors.secondarySystemGroupedBackground),
                          ),
                          child: PersonAvatar(name: p.name, me: p.isMe, size: faces.length == 1 ? 34 : 24),
                        ),
                      ),
                  ],
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            [hm(c.start), ?(c.app ?? c.place)].join(' · '),
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: Ios.headline(context).copyWith(fontSize: 15),
                          ),
                        ),
                        Text(duration(c.minutes), style: Ios.footnote(context)),
                      ],
                    ),
                    Row(
                      children: [
                        Icon(typeIcon(type), size: 12, color: resolve(context, CupertinoColors.secondaryLabel)),
                        const SizedBox(width: 4),
                        Expanded(
                          child: Text(
                            (others.isEmpty ? ['Solo tu'] : others.map((p) => p.name)).join(', '),
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: Ios.footnote(context).copyWith(color: resolve(context, CupertinoColors.label)),
                          ),
                        ),
                      ],
                    ),
                    Text(
                      c.snippet.isEmpty ? ' ' : '«${c.snippet}»',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: Ios.footnote(context).copyWith(fontStyle: FontStyle.italic),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// The days down the right edge: touch or drag to jump. Labels as many as
/// fit: the day of the month, the month where it changes.
class _DayIndex extends StatefulWidget {
  final List<String> days;
  final ValueChanged<int> onDay;
  final ValueChanged<String?> onScrub;
  const _DayIndex({required this.days, required this.onDay, required this.onScrub});

  @override
  State<_DayIndex> createState() => _DayIndexState();
}

class _DayIndexState extends State<_DayIndex> {
  int? _at;

  void _touch(double y, double h) {
    final n = widget.days.length;
    final i = (y / h * n).floor().clamp(0, n - 1);
    if (i == _at) return;
    setState(() => _at = i);
    HapticFeedback.selectionClick();
    widget.onDay(i);
    widget.onScrub(widget.days[i]);
  }

  void _end() {
    setState(() => _at = null);
    widget.onScrub(null);
  }

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, box) {
      final h = box.maxHeight, n = widget.days.length;
      final step = (n / (h / 18).floor().clamp(1, n)).ceil();
      final style = Ios.caption(
        context,
      ).copyWith(color: resolve(context, tint), fontWeight: FontWeight.w600, fontSize: 11);
      final labels = <(int, String)>[];
      String? month;
      for (var i = 0; i < n; i += step) {
        final m = shortDay(widget.days[i]).split(' ').last;
        labels.add((i, m != month && i > 0 ? m : '${DateTime.parse(widget.days[i]).day}'));
        month = m;
      }
      return GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTapDown: (d) => _touch(d.localPosition.dy, h),
        onTapUp: (_) => _end(),
        onVerticalDragStart: (d) => _touch(d.localPosition.dy, h),
        onVerticalDragUpdate: (d) => _touch(d.localPosition.dy, h),
        onVerticalDragEnd: (_) => _end(),
        onVerticalDragCancel: _end,
        child: Container(
          width: 30,
          decoration: BoxDecoration(
            color: _at == null ? null : resolve(context, CupertinoColors.systemFill),
            borderRadius: BorderRadius.circular(15),
          ),
          child: Stack(
            children: [
              for (final (i, label) in labels)
                Positioned(
                  top: i / n * h,
                  left: 0,
                  right: 0,
                  child: Text(label, textAlign: TextAlign.center, style: style),
                ),
            ],
          ),
        ),
      );
    },
  );
}
