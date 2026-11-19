//
//  ContentView.swift
//  PCMNotes
//
//  Created by Apoorv Darshan on 20/07/26.
//

import SwiftUI

private enum AppTab: Hashable {
    case physics
    case chemistry
    case mathematics
    case support
    case about

    static var captureSelection: AppTab {
        #if DEBUG && targetEnvironment(simulator)
        switch ProcessInfo.processInfo.environment["PCM_CAPTURE_SUBJECT"] {
        case "chemistry": return .chemistry
        case "mathematics": return .mathematics
        default: break
        }
        #endif
        return .physics
    }

    var accent: Color {
        switch self {
        case .physics: AppPalette.physics
        case .chemistry: AppPalette.chemistry
        case .mathematics: AppPalette.mathematics
        case .support: AppPalette.support
        case .about: AppPalette.about
        }
    }
}

struct ContentView: View {
    @State private var selection: AppTab = AppTab.captureSelection
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage(AppAppearance.storageKey) private var storedAppearance = AppAppearance.system.rawValue

    private var appearance: AppAppearance {
        AppAppearance(rawValue: storedAppearance) ?? .system
    }

    var body: some View {
        TabView(selection: $selection) {
            SubjectScene(subject: .physics)
            .tabItem {
                Label("Physics", systemImage: "atom")
            }
            .tag(AppTab.physics)

            SubjectScene(subject: .chemistry)
            .tabItem {
                Label("Chemistry", systemImage: "flask.fill")
            }
            .tag(AppTab.chemistry)

            SubjectScene(subject: .mathematics)
            .tabItem {
                Label("Maths", systemImage: "function")
            }
            .tag(AppTab.mathematics)

            NavigationStack {
                SupportView()
            }
            .tabItem {
                Label("Support", systemImage: "questionmark.circle.fill")
            }
            .tag(AppTab.support)

            NavigationStack {
                AboutView()
            }
            .tabItem {
                Label("About", systemImage: "info.circle.fill")
            }
            .tag(AppTab.about)
        }
        .tint(selection.accent)
        .persistentTabBar()
        .preferredColorScheme(appearance.colorScheme)
        .onChange(of: scenePhase, initial: true) { _, phase in
            if phase == .active {
                ReviewPrompt.recordAppOpen()
            }
        }
    }
}

#Preview {
    ContentView()
        .environmentObject(TipStore())
}
