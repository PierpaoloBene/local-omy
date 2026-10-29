/// Pieces shared by the pages, in the iOS style: formatting in Italian,
/// system colors, avatars, inset-grouped rows, alerts and action sheets.
library;

import 'package:flutter/cupertino.dart';

import '../api/mac_api.dart';

const tint = CupertinoColors.systemIndigo;

const _weekdays = ['lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica'];
const _months = [
  'gennaio',
  'febbraio',
  'marzo',
  'aprile',
  'maggio',
  'giugno',
  'luglio',
  'agosto',
  'settembre',
  'ottobre',
  'novembre',
  'dicembre',
];

String todayIso() => DateTime.now().toIso8601String().substring(0, 10);

/// "Oggi", "Ieri", or "Mercoledì 29 ottobre" (with the year if not this one).
String longDay(String iso, {bool relative = true}) {
  final d = DateTime.parse(iso);
  final today = DateTime.parse(todayIso());
  final ago = today.difference(d).inDays;
  if (relative && ago == 0) return 'Oggi';
  if (relative && ago == 1) return 'Ieri';
  final s = '${_weekdays[d.weekday - 1]} ${d.day} ${_months[d.month - 1]}${d.year == today.year ? '' : ' ${d.year}'}';
  return s[0].toUpperCase() + s.substring(1);
}

/// "29 ott".
String shortDay(String iso) {
  final d = DateTime.parse(iso);
  return '${d.day} ${_months[d.month - 1].substring(0, 3)}';
}

/// "HH:MM" of a local ISO time as the Mac writes it.
String hm(String iso) => iso.length >= 16 ? iso.substring(11, 16) : iso;

String duration(num minutes) => minutes < 1
    ? '<1 min'
    : minutes < 90
    ? '${minutes.round()} min'
    : '${(minutes / 60).toStringAsFixed(1).replaceAll('.', ',')} h';

String meters(int m) => m < 1000 ? '$m m' : '${(m / 1000).toStringAsFixed(1).replaceAll('.', ',')} km';

Color resolve(BuildContext context, Color c) => CupertinoDynamicColor.resolve(c, context);

/// Text styles after the iOS ones (body 17, subheadline 15, footnote 13).
abstract final class Ios {
  static TextStyle body(BuildContext context) => CupertinoTheme.of(context).textTheme.textStyle;
  static TextStyle headline(BuildContext context) => body(context).copyWith(fontWeight: FontWeight.w600);
  static TextStyle title(BuildContext context) =>
      body(context).copyWith(fontSize: 22, fontWeight: FontWeight.w700, letterSpacing: -0.3);
  static TextStyle subhead(BuildContext context) => body(context).copyWith(fontSize: 15, letterSpacing: -0.2);
  static TextStyle footnote(BuildContext context) => body(
    context,
  ).copyWith(fontSize: 13, color: resolve(context, CupertinoColors.secondaryLabel), letterSpacing: -0.1);
  static TextStyle caption(BuildContext context) =>
      body(context).copyWith(fontSize: 12, color: resolve(context, CupertinoColors.secondaryLabel));
}

const _palette = [
  CupertinoColors.systemOrange,
  CupertinoColors.systemGreen,
  CupertinoColors.systemTeal,
  CupertinoColors.systemBlue,
  CupertinoColors.systemPurple,
  CupertinoColors.systemPink,
  CupertinoColors.systemRed,
  CupertinoColors.systemBrown,
  CupertinoColors.systemMint,
  CupertinoColors.systemCyan,
];

/// A stable system color per name; you are the tint.
Color personColor(BuildContext context, String? name, {bool me = false}) {
  if (me) return resolve(context, tint);
  if (name == null) return resolve(context, CupertinoColors.systemGrey);
  return resolve(context, _palette[name.codeUnits.fold(0, (a, c) => (a * 31 + c) & 0xffff) % _palette.length]);
}

/// Like Contacts: initials on a gradient.
class PersonAvatar extends StatelessWidget {
  final String? name;
  final bool me;
  final double size;
  const PersonAvatar({super.key, required this.name, this.me = false, this.size = 36});

  @override
  Widget build(BuildContext context) {
    final c = personColor(context, name, me: me);
    final initial = name == null || name!.isEmpty ? '?' : name!.characters.first.toUpperCase();
    return Container(
      width: size,
      height: size,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [Color.lerp(c, const Color(0xFFFFFFFF), 0.25)!, c],
        ),
      ),
      child: Text(
        initial,
        style: TextStyle(color: const Color(0xFFFFFFFF), fontWeight: FontWeight.w600, fontSize: size * 0.44),
      ),
    );
  }
}

/// The colored rounded square of the iOS Settings rows.
class IconSquare extends StatelessWidget {
  final IconData icon;
  final Color color;
  final double size;
  const IconSquare(this.icon, this.color, {super.key, this.size = 29});

  @override
  Widget build(BuildContext context) => Container(
    width: size,
    height: size,
    decoration: BoxDecoration(color: resolve(context, color), borderRadius: BorderRadius.circular(size * 0.24)),
    child: Icon(icon, color: const Color(0xFFFFFFFF), size: size * 0.64),
  );
}

/// A row of an inset-grouped list: subtitle on several lines, highlight on
/// tap, a chevron when it opens something.
class IosRow extends StatefulWidget {
  final Widget? leading;
  final String title;
  final TextStyle? titleStyle;
  final String? subtitle;
  final Widget? below;
  final String? value;
  final Widget? trailing;
  final bool chevron;
  final VoidCallback? onTap;
  final bool destructive;

  const IosRow({
    super.key,
    this.leading,
    required this.title,
    this.titleStyle,
    this.subtitle,
    this.below,
    this.value,
    this.trailing,
    this.chevron = false,
    this.onTap,
    this.destructive = false,
  });

  @override
  State<IosRow> createState() => _IosRowState();
}

class _IosRowState extends State<IosRow> {
  bool _down = false;

  @override
  Widget build(BuildContext context) {
    final w = widget;
    final titleStyle =
        w.titleStyle ??
        Ios.body(context).copyWith(
          color: w.destructive
              ? resolve(context, CupertinoColors.systemRed)
              : w.onTap != null && w.leading == null && !w.chevron && w.trailing == null
              ? resolve(context, tint)
              : null,
        );
    final row = Container(
      color: _down ? resolve(context, CupertinoColors.systemGrey4) : null,
      constraints: const BoxConstraints(minHeight: 44),
      padding: const EdgeInsets.fromLTRB(16, 10, 14, 10),
      child: Row(
        children: [
          if (w.leading != null) ...[w.leading!, const SizedBox(width: 14)],
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(w.title, style: titleStyle),
                if (w.subtitle != null) ...[const SizedBox(height: 2), Text(w.subtitle!, style: Ios.footnote(context))],
                ?w.below,
              ],
            ),
          ),
          if (w.value != null) ...[
            const SizedBox(width: 8),
            Text(w.value!, style: Ios.body(context).copyWith(color: resolve(context, CupertinoColors.secondaryLabel))),
          ],
          if (w.trailing != null) ...[const SizedBox(width: 8), w.trailing!],
          if (w.chevron) ...[
            const SizedBox(width: 6),
            Icon(CupertinoIcons.chevron_forward, size: 17, color: resolve(context, CupertinoColors.tertiaryLabel)),
          ],
        ],
      ),
    );
    if (w.onTap == null) return row;
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTapDown: (_) => setState(() => _down = true),
      onTapCancel: () => setState(() => _down = false),
      onTapUp: (_) => setState(() => _down = false),
      onTap: w.onTap,
      child: row,
    );
  }
}

/// An inset-grouped section with iOS header and footer.
class Section extends StatelessWidget {
  final String? header;
  final String? footer;
  final List<Widget> children;
  final bool indentDividers;
  const Section({super.key, this.header, this.footer, required this.children, this.indentDividers = true});

  @override
  Widget build(BuildContext context) => CupertinoListSection.insetGrouped(
    header: header == null ? null : Text(header!.toUpperCase()),
    footer: footer == null ? null : Text(footer!, style: Ios.footnote(context)),
    margin: const EdgeInsets.fromLTRB(16, 0, 16, 8),
    dividerMargin: indentDividers ? 14 : 0,
    additionalDividerMargin: indentDividers ? 50 : 0,
    children: children,
  );
}

/// A rounded card on the grouped background, for what is not a list.
class InsetCard extends StatelessWidget {
  final Widget child;
  final EdgeInsets padding;
  final Color? color;
  final VoidCallback? onTap;
  const InsetCard({super.key, required this.child, this.padding = const EdgeInsets.all(16), this.color, this.onTap});

  @override
  Widget build(BuildContext context) {
    final card = Container(
      margin: const EdgeInsets.symmetric(horizontal: 16),
      padding: padding,
      decoration: BoxDecoration(
        color: color ?? resolve(context, CupertinoColors.secondarySystemGroupedBackground),
        borderRadius: BorderRadius.circular(12),
      ),
      child: child,
    );
    return onTap == null
        ? card
        : CupertinoButton(padding: EdgeInsets.zero, pressedOpacity: 0.7, onPressed: onTap, child: card);
  }
}

/// A small rounded label (a person, the battery).
class Pill extends StatelessWidget {
  final String text;
  final Widget? leading;
  final Color? color;
  const Pill(this.text, {super.key, this.leading, this.color});

  @override
  Widget build(BuildContext context) => Container(
    padding: EdgeInsets.fromLTRB(leading == null ? 10 : 4, 4, 10, 4),
    decoration: BoxDecoration(
      color: color ?? resolve(context, CupertinoColors.tertiarySystemFill),
      borderRadius: BorderRadius.circular(20),
    ),
    child: Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (leading != null) ...[leading!, const SizedBox(width: 6)],
        Flexible(
          child: Text(text, overflow: TextOverflow.ellipsis, style: Ios.subhead(context).copyWith(fontSize: 14)),
        ),
      ],
    ),
  );
}

/// A note at the top of a page (a feature off, what is waiting).
class Notice extends StatelessWidget {
  final IconData icon;
  final String text;
  final bool error;
  const Notice({super.key, required this.icon, required this.text, this.error = false});

  @override
  Widget build(BuildContext context) {
    final c = resolve(context, error ? CupertinoColors.systemRed : CupertinoColors.systemOrange);
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: InsetCard(
        color: c.withValues(alpha: 0.13),
        padding: const EdgeInsets.all(14),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(icon, color: c, size: 22),
            const SizedBox(width: 12),
            Expanded(child: Text(text, style: Ios.subhead(context))),
          ],
        ),
      ),
    );
  }
}

/// A thin rounded progress bar (iOS has no Cupertino one).
class ProgressBar extends StatelessWidget {
  final double? value;
  const ProgressBar(this.value, {super.key});

  @override
  Widget build(BuildContext context) => ClipRRect(
    borderRadius: BorderRadius.circular(3),
    child: SizedBox(
      height: 6,
      child: Stack(
        children: [
          Container(color: resolve(context, CupertinoColors.systemFill)),
          FractionallySizedBox(
            widthFactor: value ?? 0.1,
            child: Container(color: resolve(context, tint)),
          ),
        ],
      ),
    ),
  );
}

/// The large-title page every tab uses: iOS pull to refresh, a spinner in
/// the bar while a change runs on the Mac.
class TabScaffold extends StatelessWidget {
  final String title;
  final String? previousPageTitle;
  final List<Widget> slivers;
  final Future<void> Function()? onRefresh;
  final Widget? trailing;
  final bool busy;
  final Widget? searchField;
  const TabScaffold({
    super.key,
    required this.title,
    required this.slivers,
    this.previousPageTitle,
    this.onRefresh,
    this.trailing,
    this.busy = false,
    this.searchField,
  });

  @override
  Widget build(BuildContext context) {
    final right = busy ? const CupertinoActivityIndicator() : trailing;
    return CupertinoPageScaffold(
      backgroundColor: CupertinoColors.systemGroupedBackground,
      child: CustomScrollView(
        physics: const AlwaysScrollableScrollPhysics(parent: BouncingScrollPhysics()),
        slivers: [
          if (searchField != null)
            CupertinoSliverNavigationBar.search(
              largeTitle: Text(title),
              previousPageTitle: previousPageTitle,
              trailing: right,
              searchField: searchField!,
            )
          else
            CupertinoSliverNavigationBar(
              largeTitle: Text(title),
              previousPageTitle: previousPageTitle,
              trailing: right,
            ),
          if (onRefresh != null) CupertinoSliverRefreshControl(onRefresh: onRefresh),
          const SliverToBoxAdapter(child: SizedBox(height: 8)),
          ...slivers,
          const SliverToBoxAdapter(child: SizedBox(height: 40)),
        ],
      ),
    );
  }
}

/// While loading, or the error with a retry.
class LoadState extends StatelessWidget {
  final bool loading;
  final Object? error;
  final VoidCallback onRetry;
  const LoadState({super.key, required this.loading, required this.error, required this.onRetry});

  @override
  Widget build(BuildContext context) => SliverFillRemaining(
    hasScrollBody: false,
    child: Padding(
      padding: const EdgeInsets.all(32),
      child: loading
          ? const Center(child: CupertinoActivityIndicator(radius: 14))
          : Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(CupertinoIcons.wifi_slash, size: 44, color: resolve(context, CupertinoColors.tertiaryLabel)),
                const SizedBox(height: 12),
                Text('$error', textAlign: TextAlign.center, style: Ios.subhead(context)),
                CupertinoButton(onPressed: onRetry, child: const Text('Riprova')),
              ],
            ),
    ),
  );
}

/// Centered gray text, for an empty list.
class Empty extends StatelessWidget {
  final String text;
  const Empty(this.text, {super.key});

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(32, 4, 32, 20),
    child: Text(text, textAlign: TextAlign.center, style: Ios.footnote(context)),
  );
}

Future<void> showMessage(BuildContext context, Object message, {String? title}) => showCupertinoDialog(
  context: context,
  builder: (context) => CupertinoAlertDialog(
    title: title == null ? null : Text(title),
    content: Text('$message'),
    actions: [
      CupertinoDialogAction(isDefaultAction: true, onPressed: () => Navigator.pop(context), child: const Text('OK')),
    ],
  ),
);

/// Runs a change on the Mac with [setBusy] around it; an error as an alert.
Future<bool> change(BuildContext context, void Function(bool) setBusy, Future<void> Function() body) async {
  setBusy(true);
  try {
    await body();
    return true;
  } on MacError catch (e) {
    if (context.mounted) showMessage(context, e, title: 'Non riuscito');
    return false;
  } finally {
    setBusy(false);
  }
}

/// A one-line text prompt (rename), null if cancelled.
Future<String?> askName(BuildContext context, {required String title, String initial = '', String? message}) {
  final c = TextEditingController(text: initial);
  return showCupertinoDialog<String>(
    context: context,
    builder: (context) => CupertinoAlertDialog(
      title: Text(title),
      content: Column(
        children: [
          if (message != null) Padding(padding: const EdgeInsets.only(bottom: 10), child: Text(message)),
          const SizedBox(height: 6),
          CupertinoTextField(
            controller: c,
            autofocus: true,
            placeholder: 'Nome',
            textCapitalization: TextCapitalization.words,
            clearButtonMode: OverlayVisibilityMode.editing,
            onSubmitted: (v) => Navigator.pop(context, v.trim()),
          ),
        ],
      ),
      actions: [
        CupertinoDialogAction(onPressed: () => Navigator.pop(context), child: const Text('Annulla')),
        CupertinoDialogAction(
          isDefaultAction: true,
          onPressed: () => Navigator.pop(context, c.text.trim()),
          child: const Text('Salva'),
        ),
      ],
    ),
  );
}

Future<bool> confirm(
  BuildContext context, {
  required String title,
  required String message,
  String action = 'Elimina',
  bool destructive = true,
}) async =>
    await showCupertinoDialog<bool>(
      context: context,
      builder: (context) => CupertinoAlertDialog(
        title: Text(title),
        content: Text(message),
        actions: [
          CupertinoDialogAction(onPressed: () => Navigator.pop(context, false), child: const Text('Annulla')),
          CupertinoDialogAction(
            isDestructiveAction: destructive,
            isDefaultAction: !destructive,
            onPressed: () => Navigator.pop(context, true),
            child: Text(action),
          ),
        ],
      ),
    ) ??
    false;

/// An action sheet; the value of the action chosen, null if cancelled.
Future<T?> chooseAction<T>(
  BuildContext context, {
  String? title,
  String? message,
  required List<(T value, String label, bool destructive)> actions,
}) => showCupertinoModalPopup<T>(
  context: context,
  builder: (context) => CupertinoActionSheet(
    title: title == null ? null : Text(title),
    message: message == null ? null : Text(message),
    actions: [
      for (final (value, label, destructive) in actions)
        CupertinoActionSheetAction(
          isDestructiveAction: destructive,
          onPressed: () => Navigator.pop(context, value),
          child: Text(label),
        ),
    ],
    cancelButton: CupertinoActionSheetAction(onPressed: () => Navigator.pop(context), child: const Text('Annulla')),
  ),
);

/// A sheet page with Annulla and a bold action in its bar, like the iOS
/// editors (new contact, new event).
class SheetPage extends StatelessWidget {
  final String title;
  final String action;
  final VoidCallback onAction;
  final List<Widget> children;
  final ScrollController? controller;
  const SheetPage({
    super.key,
    required this.title,
    required this.action,
    required this.onAction,
    required this.children,
    this.controller,
  });

  @override
  Widget build(BuildContext context) => CupertinoPageScaffold(
    backgroundColor: CupertinoColors.systemGroupedBackground,
    navigationBar: CupertinoNavigationBar(
      automaticallyImplyLeading: false,
      leading: CupertinoButton(
        padding: EdgeInsets.zero,
        onPressed: () => Navigator.pop(context),
        child: const Text('Annulla'),
      ),
      middle: Text(title),
      trailing: CupertinoButton(
        padding: EdgeInsets.zero,
        onPressed: onAction,
        child: Text(action, style: const TextStyle(fontWeight: FontWeight.w600)),
      ),
    ),
    child: SafeArea(
      child: ListView(controller: controller, padding: const EdgeInsets.only(top: 20), children: children),
    ),
  );
}
