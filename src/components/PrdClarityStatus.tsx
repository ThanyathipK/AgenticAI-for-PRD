import { useMemo } from 'react';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';
import { assessPrdClarity } from '../utils/prdClarity';

export function PrdClarityStatus({ markdown }: { markdown: string }) {
  const assessment = useMemo(() => assessPrdClarity(markdown), [markdown]);
  const high = assessment.level === 'High';
  const low = assessment.level === 'Low';
  const color = high
    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
    : low ? 'border-red-200 bg-red-50 text-red-700' : 'border-amber-200 bg-amber-50 text-amber-700';

  return (
    <div className="no-print">
      <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-bold ${color}`}>
        {high ? <CheckCircle2 className="h-3 w-3" /> : <AlertTriangle className="h-3 w-3" />}
        Clarity: {assessment.level}
      </span>
    </div>
  );
}
