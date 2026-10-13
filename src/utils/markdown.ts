// Pure markdown helper functions extracted from the former Dashboard.tsx.
// These perform string-only transformations (no JSX) and are consumed by
// MarkdownRenderer and the useProjectState hook.
import { UserStory, PRDSection } from '../components/types';

type SectionSource = {
  content?: unknown;
  markdown?: unknown;
  title?: unknown;
  name?: unknown;
};

export function getSafeSectionContent(content: unknown): string {
  if (content === null || content === undefined) return "";
  if (typeof content === 'string') return content;
  if (typeof content === 'object') {
    const source = content as SectionSource;
    if (source.content !== undefined) return getSafeSectionContent(source.content);
    if (source.markdown !== undefined) return getSafeSectionContent(source.markdown);
    try {
      return Object.entries(source as Record<string, unknown>)
        .map(([key, val]) => {
          const valStr = typeof val === 'object' ? JSON.stringify(val) : String(val);
          return `**${key}:** ${valStr}`;
        })
        .join('\n\n');
    } catch (e) {
      return JSON.stringify(content);
    }
  }
  return String(content);
}

export function getSafeSectionTitle(title: unknown): string {
  if (title === null || title === undefined) return "Untitled Section";
  if (typeof title === 'string') return title;
  if (typeof title === 'object') {
    const source = title as SectionSource;
    if (source.title !== undefined) return getSafeSectionTitle(source.title);
    if (source.name !== undefined) return getSafeSectionTitle(source.name);
    return JSON.stringify(title);
  }
  return String(title);
}

export function parsePRDToSections(markdown: unknown): PRDSection[] {
  if (!markdown) return [];
  const safeMarkdown = getSafeSectionContent(markdown);
  
  const sections: PRDSection[] = [];
  const lines = safeMarkdown.split('\n');
  
  let currentSectionId = 'title';
  let currentSectionTitle = 'Product Requirement Document';
  let currentContent: string[] = [];
  
  for (const line of lines) {
    const trimmed = line.trim();
    // Template sections use level-2/level-3 headings ("### Stakeholders",
    // "### 1. Business & Strategic Overview"), so treat BOTH as section
    // boundaries — otherwise the template's front-matter tables collapse into
    // one giant undifferentiated block.
    const headingBoundary = trimmed.match(/^(#{2,3})(?:\s+(.*)|$)/);
    if (headingBoundary) {
      // Start new section
      // Pandoc drops the text from the template's centred
      // `\section*{\makebox[...,]{Contents}}` heading and emits a bare `###`.
      // Keep that derived page as the canonical, independently lockable
      // Contents section instead of creating an anonymous `section` part.
      const parsedTitle = headingBoundary[2]?.trim() ?? '';
      const title = parsedTitle || 'Contents';
      // Determine ID from title
      let nextSectionId: string;
      if (title.toLowerCase().includes('nimble by krungsri')) {
        nextSectionId = 'title';
      } else if (title.toLowerCase().includes('executive summary')) {
        nextSectionId = 'exec_summary';
      } else if (title.toLowerCase().includes('technical architecture') || title.toLowerCase().includes('technical infrastructure') || title.toLowerCase().includes('technical constraints')) {
        nextSectionId = 'tech_arch';
      } else if (title.toLowerCase().includes('scope of requirements') || title.toLowerCase().includes('user stories')) {
        nextSectionId = 'user_stories';
      } else if (title.toLowerCase().includes('stakeholder')) {
        nextSectionId = 'stakeholders';
      } else if (title.toLowerCase().includes('version history')) {
        nextSectionId = 'version_history';
      } else if (title.toLowerCase().includes('review')) {
        nextSectionId = 'reviews';
      } else if (title.toLowerCase().includes('contents')) {
        nextSectionId = 'contents';
      } else if (title.toLowerCase().includes('business & strategic')) {
        nextSectionId = 'business_overview';
      } else if (title.toLowerCase().includes('product scope') || title.toLowerCase().includes('functional requirement')) {
        nextSectionId = 'product_scope';
      } else if (title.toLowerCase().includes('technical & operational')) {
        nextSectionId = 'tech_ops';
      } else if (title.toLowerCase().includes('appendix')) {
        nextSectionId = 'appendix';
      } else {
        nextSectionId =
          title
            .toLowerCase()
            .replace(/\*/g, '')
            .replace(/[^a-z0-9]+/g, '_')
            .replace(/^_+|_+$/g, '') || 'section';
      }
      // The markdown fallback represents the cover title as ``## Nimble by
      // Krungsri``. It belongs to the existing front-matter section, rather
      // than becoming an uneditable tenth part.
      if (nextSectionId === 'title' && currentSectionId === 'title') {
        currentContent.push(line);
        continue;
      }
      // Save previous section if it has content.
      sections.push({
        id: currentSectionId,
        title: currentSectionTitle,
        content: currentContent.join('\n').trim()
      });
      currentSectionTitle = title;
      currentSectionId = nextSectionId;
      currentContent = [line];
    } else {
      currentContent.push(line);
    }
  }
  
  // Save last section
  sections.push({
    id: currentSectionId,
    title: currentSectionTitle,
    content: currentContent.join('\n').trim()
  });
  
  return sections.filter(s => s.content !== '' || s.id === 'title');
}

export function stitchSectionsToPRD(sectionsList: PRDSection[]): string {
  return sectionsList.map(s => s.content).join('\n\n');
}

export function formatUserStoriesToMarkdown(userStories: UserStory[]): string {
  let md = `## 3. Scope of Requirements (User Stories)\n\n`;
  if (!userStories || userStories.length === 0) {
    md += `No user stories registered yet.`;
    return md;
  }

  const cell = (value: string) => String(value ?? '')
    .replace(/\|/g, '\\|')
    .replace(/\r?\n/g, '<br>')
    .trim();

  md += `|  | **User Story (System Focus)** | **Functional Requirement (FR)** | **Acceptance Criteria** |\n`;
  md += `| --- | --- | --- | --- |\n`;
  userStories.forEach((us, index) => {
    const storyFocus = [
      `As a ${cell(us.as_a)}`,
      `I want to ${cell(us.i_want_to)}`,
      `So that ${cell(us.so_that)}`,
    ].join('<br><br>');
    const acceptanceCriteria = (us.acceptance_criteria || [])
      .map((ac, acIndex) => `AC ${index + 1}.${acIndex + 1}: ${cell(ac)}`)
      .join('<br>') || 'TBD';
    md += `|  | ${storyFocus} | FR ${index + 1}.1: ${cell(us.i_want_to)} | ${acceptanceCriteria} |\n`;
  });

  return md;
}
