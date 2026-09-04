import { useActions, useValues } from 'kea'

import { LemonSelect } from '@posthog/lemon-ui'

import { funnelDataLogic } from 'scenes/funnels/funnelDataLogic'

import { FunnelsFilter } from '~/queries/schema/schema-general'
import { BreakdownAttributionType, EditorFilterProps, StepOrderValue } from '~/types'

import { FUNNEL_STEP_COUNT_LIMIT } from './FunnelsQuerySteps'

const HOLD_CONSTANT = 'hold_constant'
type HoldConstant = typeof HOLD_CONSTANT

export function Attribution({ insightProps }: EditorFilterProps): JSX.Element {
    const { insightFilter, steps, breakdownFilter } = useValues(funnelDataLogic(insightProps))
    const { updateInsightFilter } = useActions(funnelDataLogic(insightProps))

    const { breakdownAttributionType, breakdownAttributionValue, funnelOrderType, funnelHoldConstantBreakdown } =
        (insightFilter || {}) as FunnelsFilter

    // Holding the property constant is not one of the attribution modes, but it answers the same
    // question the user is asking here — which value of the breakdown does a step belong to — so it
    // lives in the same control rather than as a switch beside it.
    const currentValue: HoldConstant | BreakdownAttributionType | `${BreakdownAttributionType.Step}/${number}` =
        funnelHoldConstantBreakdown
            ? HOLD_CONSTANT
            : !breakdownAttributionType
              ? BreakdownAttributionType.FirstTouch
              : breakdownAttributionType === BreakdownAttributionType.Step
                ? `${breakdownAttributionType}/${breakdownAttributionValue || 0}`
                : breakdownAttributionType

    // There is nothing to hold constant without a property to hold; the backend rejects it too.
    const hasBreakdown = !!breakdownFilter?.breakdown

    return (
        <LemonSelect
            value={currentValue}
            placeholder="Attribution"
            options={[
                { value: BreakdownAttributionType.FirstTouch, label: 'First touchpoint' },
                { value: BreakdownAttributionType.LastTouch, label: 'Last touchpoint' },
                { value: BreakdownAttributionType.AllSteps, label: 'All steps' },
                {
                    value: BreakdownAttributionType.Step,
                    label: 'Any step',
                    hidden: funnelOrderType !== StepOrderValue.UNORDERED,
                },
                {
                    value: HOLD_CONSTANT,
                    label: 'Hold constant',
                    labelInMenu: (
                        <div className="max-w-100">
                            <div>Hold constant</div>
                            <div className="text-secondary text-xs">
                                One funnel, not one per value. A person only converts if a single value carries them
                                through every step — viewed product X then bought product X, never bought Y.
                            </div>
                        </div>
                    ),
                    // Visible but disabled rather than hidden: it is the only way a user finds out
                    // the option exists and what it needs.
                    disabledReason: hasBreakdown ? undefined : 'Pick a breakdown property to hold constant first',
                },
                {
                    label: 'Specific step',
                    options: Array(FUNNEL_STEP_COUNT_LIMIT)
                        .fill(null)
                        .map((_, stepIndex) => ({
                            value: `${BreakdownAttributionType.Step}/${stepIndex}`,
                            label: `Step ${stepIndex + 1}`,
                            hidden: stepIndex >= steps.length,
                        })),
                    hidden: funnelOrderType === StepOrderValue.UNORDERED,
                },
            ]}
            onChange={(value) => {
                if (!value) {
                    return
                }
                if (value === HOLD_CONSTANT) {
                    // Attribution is forced to all_events server-side, so it is left untouched here
                    // and comes back as the user left it when they switch away again.
                    updateInsightFilter({ funnelHoldConstantBreakdown: true })
                    return
                }
                const [breakdownAttributionType, breakdownAttributionValue] = value.split('/')
                updateInsightFilter({
                    breakdownAttributionType: breakdownAttributionType as BreakdownAttributionType,
                    breakdownAttributionValue: breakdownAttributionValue ? parseInt(breakdownAttributionValue) : 0,
                    funnelHoldConstantBreakdown: false,
                })
            }}
            dropdownMaxContentWidth={true}
            data-attr="breakdown-attributions"
        />
    )
}
