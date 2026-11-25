import StoreKit
import SwiftUI

struct SupportView: View {
    private let supportEmail = URL(
        string: "mailto:ad13dtu@gmail.com?subject=PCM%20Notes%20Support"
    )!
    private let xProfile = URL(string: "https://x.com/apoorvdarshan")!
    private let instagramProfile = URL(string: "https://www.instagram.com/apoorvxi/")!
    private let appStoreListing = URL(string: "https://apps.apple.com/app/id6792598227")!
    private let appStoreReview = URL(string: "https://apps.apple.com/app/id6792598227?action=write-review")!
    private let sourceRepository = URL(string: "https://github.com/apoorvdarshan/pcm-notes")!
    private let bugReport = URL(string: "https://github.com/apoorvdarshan/pcm-notes/issues/new?template=bug_report.yml")!
    private let featureRequest = URL(string: "https://github.com/apoorvdarshan/pcm-notes/issues/new?template=feature_request.yml")!
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.requestReview) private var requestReview
    @Environment(\.openURL) private var openURL

    var body: some View {
        ZStack {
            NotebookBackground(accent: AppPalette.support)

            ScrollView {
                VStack(alignment: .leading, spacing: 34) {
                    supportHero
                    TipJarSection()

                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .top, spacing: 30) {
                            helpColumn
                                .frame(minWidth: 300, maxWidth: 360, alignment: .top)

                            DoodleVerticalRule(color: AppPalette.support)

                            faqColumn
                                .frame(minWidth: 420, maxWidth: .infinity, alignment: .top)
                        }

                        VStack(alignment: .leading, spacing: 34) {
                            helpColumn
                            faqColumn
                        }
                    }
                }
                .readableWidth()
                .padding(.leading, 72)
                .padding(.trailing, 24)
                .padding(.top, 22)
                .padding(.bottom, 42)
            }
        }
        .navigationTitle("Support")
        .navigationBarTitleDisplayMode(.inline)
    }

    private var supportHero: some View {
        ZStack(alignment: .topTrailing) {
            VStack(alignment: .leading, spacing: 10) {
                EyebrowLabel(text: "Write it in the margin", color: AppPalette.support)

                Text("Need a hand?")
                    .font(.custom("Noteworthy-Bold", size: 42, relativeTo: .largeTitle))
                    .markerHighlight(AppPalette.support)

                Text("For a note issue, use either email address and include the subject, chapter, and PDF page. You can also message @apoorvdarshan on X.")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.trailing, dynamicTypeSize.isAccessibilitySize ? 0 : 76)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            if !dynamicTypeSize.isAccessibilitySize {
                Image(systemName: "lifepreserver")
                    .font(.system(size: 62, weight: .light))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(AppPalette.support.opacity(0.72))
                    .rotationEffect(.degrees(-9))
                    .accessibilityHidden(true)
            }
        }
    }

    private var helpColumn: some View {
        VStack(alignment: .leading, spacing: 24) {
            VStack(alignment: .leading, spacing: 0) {
                DoodleSectionTitle(
                    title: "Get help",
                    color: AppPalette.support,
                    annotation: "tell me where"
                )

                NotebookActionRow(
                    title: "Email support",
                    detail: "ad13dtu@gmail.com",
                    symbol: "envelope",
                    color: AppPalette.support,
                    destination: supportEmail
                )
                .accessibilityIdentifier("support.email")

                NotebookActionRow(
                    title: "Report a bug",
                    detail: "Open an issue on GitHub",
                    symbol: "ladybug",
                    color: AppPalette.support,
                    destination: bugReport
                )
                .accessibilityIdentifier("support.bug")

                NotebookActionRow(
                    title: "Request a feature",
                    detail: "Suggest an idea on GitHub",
                    symbol: "lightbulb",
                    color: AppPalette.support,
                    destination: featureRequest
                )
                .accessibilityIdentifier("support.feature")
            }

            VStack(alignment: .leading, spacing: 0) {
                DoodleSectionTitle(
                    title: "Open source",
                    color: .primary,
                    annotation: "MIT licence"
                )

                NotebookActionRow(
                    title: "Star on GitHub",
                    detail: "apoorvdarshan/pcm-notes · the app's source code",
                    symbol: "chevron.left.forwardslash.chevron.right",
                    color: .primary,
                    destination: sourceRepository
                )
                .accessibilityIdentifier("support.github")
            }

            VStack(alignment: .leading, spacing: 0) {
                DoodleSectionTitle(
                    title: "Follow along",
                    color: AppPalette.mathematicsSecondary,
                    annotation: "updates + messages"
                )

                NotebookActionRow(
                    title: "X (Twitter)",
                    detail: "@apoorvdarshan",
                    symbol: "at",
                    color: AppPalette.mathematicsSecondary,
                    destination: xProfile
                )
                .accessibilityIdentifier("support.x")

                NotebookActionRow(
                    title: "Instagram",
                    detail: "@apoorvxi",
                    symbol: "camera",
                    color: AppPalette.about,
                    destination: instagramProfile
                )
                .accessibilityIdentifier("support.instagram")
            }

            VStack(alignment: .leading, spacing: 0) {
                DoodleSectionTitle(
                    title: "Spread the word",
                    color: AppPalette.chemistry,
                    annotation: "it really helps"
                )

                Button(action: rate) {
                    NotebookRowLabel(
                        title: "Rate PCM Notes",
                        detail: "Leave a rating or review on the App Store",
                        symbol: "star",
                        trailingSymbol: "star.bubble",
                        color: AppPalette.chemistry
                    )
                }
                .buttonStyle(.plain)
                .accessibilityHint("Asks for your App Store rating")
                .accessibilityIdentifier("support.rate")

                ShareLink(
                    item: appStoreListing,
                    subject: Text("PCM Notes"),
                    message: Text("Handwritten Physics, Chemistry and Maths notes for JEE and NEET, offline on iPhone and iPad.")
                ) {
                    NotebookRowLabel(
                        title: "Share PCM Notes",
                        detail: "Send the App Store link to a friend",
                        symbol: "square.and.arrow.up",
                        trailingSymbol: "arrowshape.turn.up.right",
                        color: AppPalette.chemistry
                    )
                }
                .buttonStyle(.plain)
                .accessibilityHint("Opens the share sheet")
                .accessibilityIdentifier("support.share")
            }
        }
    }

    /// Shows Apple's in-app rating prompt, or the App Store review page when the
    /// prompt was already requested recently (see `ReviewPrompt`).
    private func rate() {
        let lastRequest = UserDefaults.standard.double(forKey: ReviewPrompt.Key.lastRequest)
        if ReviewPrompt.shouldRequestInAppPrompt(lastRequest: lastRequest, now: Date()) {
            ReviewPrompt.recordRequest()
            requestReview()
        } else {
            openURL(appStoreReview)
        }
    }

    private var faqColumn: some View {
        VStack(alignment: .leading, spacing: 0) {
            DoodleSectionTitle(
                title: "Common questions",
                color: AppPalette.support,
                annotation: "tap to unfold"
            )

            FAQRow(
                number: 1,
                question: "Does the app work without internet?",
                answer: "Yes. Every handwritten chapter is bundled as a local PDF, so reading and zooming work fully offline."
            )
            FAQRow(
                number: 2,
                question: "Why can a chapter take a moment to open?",
                answer: "Long chapters contain up to 92 high-resolution pages. The PDF opens locally, but the device may need a moment to prepare those pages."
            )
            FAQRow(
                number: 3,
                question: "Is this complete NEET material?",
                answer: "Physics and Chemistry can support NEET revision. Biology is not included, so this is not a complete NEET library."
            )
            FAQRow(
                number: 4,
                question: "How do I report a wrong page?",
                answer: "Email support with the subject, chapter, and PDF page number so it can be checked quickly."
            )
        }
    }
}

private struct NotebookActionRow: View {
    let title: String
    let detail: String
    let symbol: String
    let color: Color
    let destination: URL

    var body: some View {
        Link(destination: destination) {
            NotebookRowLabel(
                title: title,
                detail: detail,
                symbol: symbol,
                trailingSymbol: "arrow.up.right",
                color: color
            )
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens an external link")
    }
}

/// The look shared by the Support tab's link, share and rate rows.
private struct NotebookRowLabel: View {
    let title: String
    let detail: String
    let symbol: String
    let trailingSymbol: String
    let color: Color

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: symbol)
                .font(.title3.weight(.semibold))
                .foregroundStyle(color)
                .frame(width: 30)

            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.headline)
                    .foregroundStyle(.primary)

                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
            }

            Spacer(minLength: 8)

            Image(systemName: trailingSymbol)
                .font(.callout.weight(.bold))
                .foregroundStyle(color)
                .rotationEffect(.degrees(5))
        }
        .padding(.vertical, 15)
        .contentShape(Rectangle())
        .overlay(alignment: .bottom) {
            DoodleRule(color: color)
        }
    }
}

private struct FAQRow: View {
    let number: Int
    let question: String
    let answer: String

    @State private var isExpanded = false

    var body: some View {
        VStack(spacing: 0) {
            DisclosureGroup(isExpanded: $isExpanded) {
                Text(answer)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.leading, 40)
                    .padding(.top, 10)
                    .padding(.bottom, 5)
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text("Q\(number)")
                        .font(.caption.monospaced().weight(.black))
                        .foregroundStyle(AppPalette.support)

                    Text(question)
                        .font(.subheadline.weight(.bold))
                        .foregroundStyle(.primary)
                        .multilineTextAlignment(.leading)
                }
            }
            .tint(AppPalette.support)
            .padding(.vertical, 17)

            DoodleRule(color: AppPalette.support)
        }
    }
}

#Preview {
    NavigationStack {
        SupportView()
    }
    .environmentObject(TipStore())
}
