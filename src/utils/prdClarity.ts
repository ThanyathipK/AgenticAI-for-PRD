export type PrdClarityLevel = 'High' | 'Medium' | 'Low';

export interface PrdClarityAssessment {
  level: PrdClarityLevel;
  score: number;
  summary: string;
  missing: string[];
}

const TEMPLATE_GUIDANCE = [
  /Define measurable performance, availability, security, privacy, recovery, and observability requirements before implementation\.?/gi,
  /Confirm peak transactions per second, concurrent users, and monthly volume before capacity planning\.?/gi,
  /Confirm the year-over-year growth forecast, planning horizon, and scaling thresholds\.?/gi,
  /Define pilot users, release stages, readiness criteria, monitoring, ownership, and rollback steps\.?/gi,
  /Confirm performance targets, integration limits, security controls, data retention, operational ownership, rollout criteria, and rollback conditions\.?/gi,
  /Confirm assumptions for external services, data availability, compliance ownership, support coverage, and deployment environments\.?/gi,
  /Reference the source requirements, architecture decisions, API specifications, compliance policies, and operating procedures used by this PRD\.?/gi,
  /Define domain terms, acronyms, system names, and business rules used in this PRD\.?/gi,
];

const cleanPrd = (markdown: string): string => TEMPLATE_GUIDANCE.reduce(
  (text, pattern) => text.replace(pattern, ' '),
  markdown,
).replace(/```[\s\S]*?```/g, ' ').replace(/[#*_`|<>\\{}[\]]/g, ' ').replace(/\s+/g, ' ').trim();

export function assessPrdClarity(markdown: string): PrdClarityAssessment {
  const text = cleanPrd(markdown || '');
  if (!text) {
    return {
      level: 'Low', score: 0,
      summary: 'The PRD does not yet contain enough project-specific content.',
      missing: ['named systems or domain', 'flows and actors', 'measurable outcomes', 'operational controls'],
    };
  }

  const namedTechnology = /\b(?:[A-Z][A-Za-z0-9.-]+(?:\s+[A-Z][A-Za-z0-9.-]+){0,3}\s+(?:API|Gateway|Service|Platform|Database|Queue|Tool|SDK)|Kafka|PostgreSQL|MySQL|Redis|Oracle|Salesforce|Kubernetes|AWS|Azure|GCP)\b/.test(text);
  const namedDomain = /\b(?:payments?|beneficiar(?:y|ies)|settlement|reconciliation|lending|loans?|cards?|KYC|AML|CFT|PDPA|SWIFT|PromptPay|core banking|treasury|merchant|fraud|identity)\b/i.test(text);
  const actorsAndFlows = /\b(?:as a|i want to|so that|given|when|then|customer|officer|administrator|approver|operator)\b/i.test(text);
  const measurementCount = [
    ...text.matchAll(/\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|minutes?|hours?|days?|%|tps|rps|transactions?|requests?|users?|records?)\b/gi),
  ].length;
  const operationalCategories = [
    /\b(?:timeout|retry|idempotenc\w*|circuit breaker)\b/i,
    /\b(?:monitor(?:ing)?|alert(?:ing)?|logging|incident)\b/i,
    /\b(?:encryption|authentication|authorization|privacy)\b/i,
    /\b(?:recovery|rollback|availability)\b/i,
    /\b(?:retention|audit trail|reconciliation)\b/i,
  ].filter((pattern) => pattern.test(text)).length;
  const constraints = /\b(?:must|shall|required|cannot|only if|at least|at most|within|before|after)\b/i.test(text);

  const score = (
    (namedTechnology ? 2 : 0) + (namedDomain ? 1 : 0) + (actorsAndFlows ? 1 : 0)
    + Math.min(measurementCount, 2) + Math.min(operationalCategories, 3) + (constraints ? 1 : 0)
  );
  const missing = [
    ...(!namedTechnology ? ['a named system, API, or tool'] : []),
    ...(!namedDomain ? ['a specific business domain'] : []),
    ...(!actorsAndFlows ? ['actors and end-to-end flows'] : []),
    ...(measurementCount < 2 ? ['at least two measurable targets or outcomes'] : []),
    ...(operationalCategories < 3 ? ['at least three distinct operational control areas'] : []),
    ...(!constraints ? ['explicit rules and constraints'] : []),
  ];
  const isHigh = namedTechnology && namedDomain && actorsAndFlows
    && measurementCount >= 2 && operationalCategories >= 3 && constraints;
  const isMedium = (namedTechnology || namedDomain) && actorsAndFlows && constraints
    && (measurementCount >= 1 || operationalCategories >= 2);
  const level: PrdClarityLevel = isHigh ? 'High' : isMedium ? 'Medium' : 'Low';
  const summary = level === 'High'
    ? 'The PRD has named technology and domain context, clear flows, multiple measurable targets, and broad operational coverage.'
    : `The PRD still needs ${missing.slice(0, 3).join(', ')}.`;

  return { level, score, summary, missing };
}
