import { Placeholder } from '../components/ui'

export default function Monitoring() {
  return (
    <Placeholder
      heading="Monitoring"
      arriving="Arrives in Sprint 2, reading the KPI contracts approved here in Sprint 1."
      bullets={[
        'Historical KPI series built from the governed formula and calendar',
        'Expected behaviour: trend, seasonality and expected range',
        'Sparse-history handling for newly launched entities, with reduced confidence',
        'Material movement detection using both statistical significance and business impact',
        'Incidents raised only when a movement clears the stored materiality thresholds',
      ]}
    />
  )
}
