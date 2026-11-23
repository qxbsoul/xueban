export interface NavigationUnit {
  id: string;
  title: string;
  point_titles: string[];
}

export interface NavigationSection {
  id: string;
  lesson?: { id: string } | null;
  knowledge_points: Array<{ title: string }>;
}

export interface UnitTarget {
  unit: NavigationUnit;
  index: number;
}

export function resolveUnitTarget(
  units: NavigationUnit[],
  title: string,
  preferredId?: string,
): UnitTarget | null {
  if (preferredId) {
    const preferredIndex = units.findIndex((unit) => unit.id === preferredId);
    if (preferredIndex >= 0) return { unit: units[preferredIndex], index: preferredIndex };
  }
  const normalized = title.replace(/\s+/g, '');
  const index = units.findIndex((unit) => unit.title === title
    || unit.point_titles.some((point) => point === title || point.replace(/\s+/g, '') === normalized));
  return index >= 0 ? { unit: units[index], index } : null;
}

export function resolveCrossSectionTarget(
  sections: NavigationSection[],
  title: string,
  currentLessonId: string,
): NavigationSection | null {
  const normalized = title.replace(/\s+/g, '');
  return sections.find((section) => section.lesson
    && section.lesson.id !== currentLessonId
    && section.knowledge_points.some((point) => point.title === title
      || point.title.replace(/\s+/g, '') === normalized)) || null;
}