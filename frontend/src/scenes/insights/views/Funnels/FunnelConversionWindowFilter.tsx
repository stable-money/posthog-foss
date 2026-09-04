import { useActions, useValues } from 'kea'

import { IconInfo } from '@posthog/icons'
import { LemonBanner, LemonInput, LemonSelect, LemonSelectOption } from '@posthog/lemon-ui'

import { dayjs } from 'lib/dayjs'
import { Tooltip } from 'lib/lemon-ui/Tooltip'
import { dateStringToDayJs } from 'lib/utils/dateFilters'
import { capitalizeFirstLetter, pluralize } from 'lib/utils/strings'
import { funnelDataLogic } from 'scenes/funnels/funnelDataLogic'
import { TIME_INTERVAL_BOUNDS } from 'scenes/funnels/funnelUtils'

import { FunnelsFilter } from '~/queries/schema/schema-general'
import { EditorFilterProps, FunnelConversionWindowTimeUnit } from '~/types'

const BOUNDARY_OPTIONS: LemonSelectOption<'clip' | 'extend'>[] = [
    { label: 'inside the date range', value: 'clip' },
    { label: 'even after the date range ends', value: 'extend' },
]

export function FunnelConversionWindowFilter({ insightProps }: Pick<EditorFilterProps, 'insightProps'>): JSX.Element {
    const {
        aggregationTargetLabel,
        querySource,
        conversionWindow,
        conversionWindowInterval,
        conversionWindowUnit,
        insightFilter,
    } = useValues(funnelDataLogic(insightProps))
    const { setConversionWindowInterval, setConversionWindowUnit, commitConversionWindow, updateInsightFilter } =
        useActions(funnelDataLogic(insightProps))

    const { funnelWindowBoundary } = (insightFilter || {}) as FunnelsFilter
    const boundary = funnelWindowBoundary || 'clip'

    const hasEdited = conversionWindowInterval !== null
    const displayInterval = hasEdited ? conversionWindowInterval || undefined : conversionWindow.funnelWindowInterval

    const displayUnit = conversionWindowUnit ?? conversionWindow.funnelWindowIntervalUnit
    const intervalBounds = TIME_INTERVAL_BOUNDS[displayUnit]

    const options: LemonSelectOption<FunnelConversionWindowTimeUnit>[] = Object.keys(TIME_INTERVAL_BOUNDS).map(
        (unit) => ({
            label: capitalizeFirstLetter(
                pluralize(conversionWindow.funnelWindowInterval ?? 7, unit, `${unit}s`, false)
            ),
            value: unit as FunnelConversionWindowTimeUnit,
        })
    )

    // date_to unset means a rolling "now" range, which is always less than a window in the past
    const dateTo = dateStringToDayJs(querySource?.dateRange?.date_to ?? null) ?? dayjs()
    const windowEnd = dateTo.add(conversionWindow.funnelWindowInterval, conversionWindow.funnelWindowIntervalUnit)
    const showClipWarning = boundary === 'clip' && windowEnd.isAfter(dayjs())

    return (
        <div className="flex flex-col gap-2" data-attr="funnel-conversion-window-filter">
            <div className="flex items-center gap-2 flex-wrap">
                <span className="flex whitespace-nowrap">
                    Conversion window limit
                    <Tooltip
                        title={
                            <>
                                Limit to {aggregationTargetLabel.plural}{' '}
                                {querySource?.aggregation_group_type_index != null ? 'that' : 'who'} converted within a
                                specific time frame. {capitalizeFirstLetter(aggregationTargetLabel.plural)}{' '}
                                {querySource?.aggregation_group_type_index != null ? 'that' : 'who'} do not convert in
                                this time frame will be considered as drop-offs.
                            </>
                        }
                    >
                        <IconInfo className="w-4 info-indicator" />
                    </Tooltip>
                </span>
                <div className="flex items-center gap-2">
                    <LemonInput
                        type="number"
                        className="max-w-20"
                        fullWidth={false}
                        min={intervalBounds[0]}
                        max={intervalBounds[1]}
                        value={displayInterval}
                        onChange={(value) => setConversionWindowInterval(value || 0)}
                        onBlur={commitConversionWindow}
                        onPressEnter={commitConversionWindow}
                    />
                    <LemonSelect
                        dropdownMatchSelectWidth={false}
                        value={displayUnit}
                        onChange={(funnelWindowIntervalUnit: FunnelConversionWindowTimeUnit | null) => {
                            if (funnelWindowIntervalUnit) {
                                setConversionWindowUnit(funnelWindowIntervalUnit)
                                commitConversionWindow()
                            }
                        }}
                        options={options}
                        data-attr="funnel-conversion-window-unit"
                    />
                </div>
                <span className="flex items-center whitespace-nowrap gap-1">
                    counting steps that land
                    <LemonSelect
                        dropdownMatchSelectWidth={false}
                        value={boundary}
                        onChange={(value) => value && updateInsightFilter({ funnelWindowBoundary: value })}
                        options={BOUNDARY_OPTIONS}
                        data-attr="funnel-conversion-window-boundary"
                    />
                    <Tooltip
                        title={
                            <>
                                <p>
                                    <b>Inside the date range</b> only counts a later step if it happened before the
                                    end of the date range. {capitalizeFirstLetter(aggregationTargetLabel.plural)} who
                                    entered near the end are counted as drop-offs even though their conversion window
                                    hasn't run out yet, so conversion reads low and moving the end date changes the
                                    answer.
                                </p>
                                <p>
                                    <b>Even after the date range ends</b> only requires the <i>first</i> step to be
                                    inside the date range, and then gives everyone their full conversion window,
                                    however far past the end date that reaches. This is what Mixpanel does.
                                </p>
                            </>
                        }
                    >
                        <IconInfo className="w-4 info-indicator" />
                    </Tooltip>
                </span>
            </div>
            {showClipWarning && (
                <LemonBanner
                    type="warning"
                    action={{
                        children: 'Switch to "even after the date range ends"',
                        onClick: () => updateInsightFilter({ funnelWindowBoundary: 'extend' }),
                    }}
                >
                    The date range ends less than{' '}
                    {pluralize(
                        conversionWindow.funnelWindowInterval,
                        conversionWindow.funnelWindowIntervalUnit,
                        `${conversionWindow.funnelWindowIntervalUnit}s`,
                        true
                    )}{' '}
                    ago, so some {aggregationTargetLabel.plural} haven't had their full conversion window yet.
                    Conversion is undercounted until it has — the true rate is higher than what's shown.
                </LemonBanner>
            )}
        </div>
    )
}
