# Liquid Glass: regole per questa app (SDK 27.2, verificato sulle .swiftinterface di SwiftUICore)

- Componenti di sistema (TabView, NavigationStack, toolbar, sheet, menu, List/Form) adottano il vetro da soli: usarli, non ricrearli.
- Vetro esplicito SOLO sullo strato di navigazione/controlli che galleggia sul contenuto; mai vetro su vetro; il contenuto scorre sotto.
- API: `glassEffect(_ glass: Glass = .regular, in: shape)`, `Glass.regular/.clear/.tint(_:)/.interactive()`, `GlassEffectContainer`,
  `glassEffectID(_:in:)`, `glassEffectUnion`, `glassEffectTransition`, `.buttonStyle(.glass)` / `.glassProminent`,
  `backgroundExtensionEffect()`, `scrollEdgeEffectStyle(_:for:)`, `ToolbarSpacer`, `ConcentricRectangle`.
- iOS 27: `tabViewBottomAccessory { }` (solo iOS), `tabBarMinimizeBehavior(.onScrollDown)`, `Tab(role: .search)`, `Tab(role: .prominent)`,
  toolbar `visibilityPriority`, `toolbarOverflowMenu`, `swipeActionsContainer()`, `CrossFadeNavigationTransition`.
- `toolbarMinimizeBehavior` NON esiste. `containerConcentric` non verificato: non usarlo.
- Accessibilità: rispettare `@Environment(\.accessibilityReduceTransparency)` (sfondo opaco) e Dynamic Type.

Scelte per local-omy:
- TabView con 5 `Tab`: Oggi, Conversazioni, Persone, Luoghi, Chiedi; `tabBarMinimizeBehavior(.onScrollDown)`.
- `tabViewBottomAccessory`: stato del ciondolo compatto (pallino stato + batteria + "in coda N") visibile in tutte le schede.
- Chiedi: barra di input flottante con `glassEffect(.regular.interactive(), in: .capsule)` in un `GlassEffectContainer` insieme al
  pulsante invio (`.buttonStyle(.glassProminent)`); messaggi con `defaultScrollAnchor(.bottom)`; chip delle citazioni normali (non vetro).
- Luoghi: SwiftUI `Map` a tutta larghezza con controlli in vetro sopra; Conversazioni: ricerca con `.searchable`.
- Colore d'accento: indigo (come oggi).
