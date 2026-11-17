import {DRILL_STATUSES,CONFIG_STATUSES} from '../../constants/enums.js';
import {esc} from '../../utils/html.js';
export const badge=(status)=>`<span class="badge ${esc(status)}">${esc(DRILL_STATUSES[status]||status)}</span>`;
export const configBadge=(status)=>`<span class="badge config ${esc(status)}">${esc(CONFIG_STATUSES[status]||status)}</span>`;
