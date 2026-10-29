import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';
import '../api/models.dart';
import 'common.dart';
import 'chat.dart';
import 'timeline.dart';
import '../l10n.dart';

/// The kinds of conversation, as omy transcripts conversations --type.
final conversationTypes = <(String, String, IconData)>[
  ('room', tr('Di persona', 'In person'), CupertinoIcons.person_2_fill),
  ('meeting', 'Online', CupertinoIcons.videocam_fill),
  ('call', tr('Chiamate', 'Calls'), CupertinoIcons.phone_fill),
  ('media', 'Video', CupertinoIcons.play_rectangle_fill),
];

IconData typeIcon(String type) =>
    conversationTypes.firstWhere((t) => t.$1 == type, orElse: () => conversationTypes.first).$3;

/// A row of rounded buttons, one selected (null: the first, [all], "Tutti").
class FilterPills<T> extends StatelessWidget {
  final List<(T, String, IconData?)> values;
  final T? selected;
  final ValueChanged<T?> onChanged;
  final String? all;
  const FilterPills({super.key, required this.values, required this.selected, required this.onChanged, this.all});

  @override
  Widget build(BuildContext context) {
    Widget pill(T? v, String label, IconData? icon) {
      final on = v == selected;
      final fg = on ? const Color(0xFFFFFFFF) : resolve(context, CupertinoColors.label);
      return Padding(
        padding: const EdgeInsets.only(right: 8),
        child: CupertinoButton(
          padding: const EdgeInsets.symmetric(horizontal: 13),
          minimumSize: const Size(0, 32),
          borderRadius: BorderRadius.circular(16),
          color: on ? resolve(context, tint) : resolve(context, CupertinoColors.secondarySystemGroupedBackground),
          onPressed: () => onChanged(on ? null : v),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (icon != null) ...[Icon(icon, size: 15, color: fg), const SizedBox(width: 5)],
              Text(
                label,
                style: Ios.subhead(context).copyWith(color: fg, fontWeight: on ? FontWeight.w600 : null),
              ),
            ],
          ),
        ),
      );
    }

    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.symmetric(horizontal: 16),
      child: Row(
        children: [
          pill(null, all ?? tr('Tutti', 'All'), null),
          for (final (v, label, icon) in values) pill(v, label, icon),
        ],
      ),
    );
  }
}

/// A conversation: when and where, who, a sentence; opens it in [list].
class ConversationRow extends StatelessWidget {
  final MacApi api;
  final Conversation c;

  /// The conversations swiped through from this one.
  final List<Conversation> list;
  final String previousPageTitle;
  const ConversationRow({
    super.key,
    required this.api,
    required this.c,
    required this.list,
    required this.previousPageTitle,
  });

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
    final faces = c.participants.take(3).toList();
    return IosRow(
      leading: SizedBox(
        width: 40,
        height: 40,
        child: Stack(
          children: [
            for (final (i, p) in faces.indexed)
              Positioned(
                left: faces.length == 1 ? 2 : i * 9.0,
                top: faces.length == 1 ? 2 : i * 5.0,
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
      title: [
        '${longDay(c.day)} ${hm(c.start)}',
        if (c.app != null) c.app! else if (c.place != null) c.place!,
      ].join(' · '),
      titleStyle: Ios.headline(context).copyWith(fontSize: 15),
      value: duration(c.minutes),
      chevron: true,
      below: Padding(
        padding: const EdgeInsets.only(top: 2),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(typeIcon(type), size: 13, color: resolve(context, CupertinoColors.secondaryLabel)),
                const SizedBox(width: 4),
                Expanded(
                  child: Text(
                    (others.isEmpty ? [tr('Solo tu', 'Just you')] : others.map((p) => p.name)).join(', '),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Ios.footnote(context),
                  ),
                ),
              ],
            ),
            if (c.snippet.isNotEmpty)
              Text(
                '«${c.snippet}»',
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: Ios.footnote(context).copyWith(fontStyle: FontStyle.italic),
              ),
          ],
        ),
      ),
      onTap: () => ConversationViewer.open(context, api, list, list.indexOf(c), previousPageTitle: previousPageTitle),
    );
  }
}

/// The conversations with a person and of a type, a page at a time (whole
/// days, about a month): the first when opened, the next ones as the
/// timeline nears its end. Kept in memory only.
class ConversationFeed extends ChangeNotifier {
  static const pageSize = 1000;

  final MacApi api;
  final String? person, type;
  ConversationFeed(this.api, {this.person, this.type});

  ConversationList? list;

  /// The last failure; with [list] still there, it is out of date.
  Object? error;
  bool _loading = false;
  bool _disposed = false;

  bool get hasMore => list?.next != null;

  /// The newest page again. The older pages already loaded stay: only the
  /// recent days change (new conversations, names given).
  Future<void> refresh() async {
    try {
      final first = await api.conversations(person: person, type: type, limit: pageSize);
      final old = list, cut = first.next;
      list = old == null || cut == null
          ? first
          : ConversationList([
              ...first.conversations,
              for (final c in old.conversations)
                if (c.day.compareTo(cut) < 0) c,
            ], old.conversations.any((c) => c.day.compareTo(cut) < 0) ? old.next : cut);
      error = null;
    } on MacError catch (e) {
      error = e;
    }
    _changed();
  }

  /// The next page, if any and not already on its way.
  Future<void> more() async {
    final next = list?.next;
    if (next == null || _loading) return;
    _loading = true;
    try {
      list = list! + await api.conversations(person: person, type: type, before: next, limit: pageSize);
      error = null;
    } on MacError catch (e) {
      error = e;
    } finally {
      _loading = false;
    }
    _changed();
  }

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}

/// Every conversation with a person or of a type, as a timeline.
class ConversationsPage extends StatefulWidget {
  final MacApi api;
  final String title;
  final String? person, type, previousPageTitle;
  const ConversationsPage({
    super.key,
    required this.api,
    required this.title,
    this.person,
    this.type,
    this.previousPageTitle,
  });

  @override
  State<ConversationsPage> createState() => _ConversationsPageState();
}

class _ConversationsPageState extends State<ConversationsPage> {
  late final _feed = ConversationFeed(widget.api, person: widget.person, type: widget.type)..refresh();

  @override
  void dispose() {
    _feed.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: _feed,
    builder: (context, _) => Timeline(
      api: widget.api,
      title: widget.title,
      previousPageTitle: widget.previousPageTitle,
      onRefresh: _feed.refresh,
      conversations: _feed.list?.conversations,
      hasMore: _feed.hasMore,
      onMore: _feed.more,
      error: _feed.error,
      onRetry: _feed.refresh,
    ),
  );
}

/// Conversations as an inset-grouped list, at most [limit] (and then a row
/// to see them all).
class ConversationSection extends StatelessWidget {
  final MacApi api;
  final ConversationList list;
  final String previousPageTitle;
  final String? header;
  final int? limit;
  final VoidCallback? onMore;
  const ConversationSection({
    super.key,
    required this.api,
    required this.list,
    required this.previousPageTitle,
    this.header,
    this.limit,
    this.onMore,
  });

  @override
  Widget build(BuildContext context) {
    final all = list.conversations;
    final shown = limit == null ? all : all.take(limit!).toList();
    final more = list.next != null;
    return Section(
      header:
          header ??
          '${counted(all.length, 'conversazione', 'conversazioni', 'conversation', 'conversations')}${more ? '+' : ''}',
      children: [
        if (all.isEmpty) IosRow(title: tr('Nessuna conversazione', 'No conversations')),
        for (final c in shown) ConversationRow(api: api, c: c, list: all, previousPageTitle: previousPageTitle),
        if (onMore != null && (all.length > shown.length || more))
          IosRow(title: tr('Mostra tutte', 'Show all'), onTap: onMore),
      ],
    );
  }
}
