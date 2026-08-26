import { Placeholder } from '../components/ui'

export default function Investigation() {
  return (
    <Placeholder
      heading="Investigation"
      arriving="Arrives in Sprint 3, using the dimensions and join-safety metadata recorded in Sprint 1."
      bullets={[
        'Contribution analysis: which dimension explains most of a KPI movement',
        'Top-K candidate selection and adaptive drill-down, stopping when the explanation suffices',
        'Entity promotion — deeper statistics only for entities that become relevant',
        'User-driven entity analysis: "tell me about this product" as a separate entry path',
        'Hidden-shift detection where the aggregate looks flat but composition changed',
      ]}
    />
  )
}
