import React, { useCallback, useEffect, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  FileText,
  Plus,
  ShieldCheck,
  UserRound,
  X,
  XCircle,
} from 'lucide-react';
import {
  AlertBanner,
  EmptyState,
  Modal,
  Spinner,
} from '../components/ui';
import {
  APPROVAL_STAGES,
  decideProvisioningStage,
  decideTowerHeadStage,
  getLicenseRequest,
  getLicenseRequestOptions,
  getProvisioningApprover,
  getTowerHeadApprover,
  getLicenseRequests,
  requestLicense,
} from '../api/services';
import { useAuth } from '../context/AuthContext';
import { RequestLicenseModal } from './LicensesPage';

const formatDateTime = (value) => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
};

// Map a backend stage identifier to its display label and configured approver.
const STAGE_META = {
  [APPROVAL_STAGES.TOWER_HEAD]: { label: 'Tower Head', approver: 'Akshayaprasanth' },
  [APPROVAL_STAGES.IT_LICENSE_PROVISIONING]: {
    label: 'IT / License Provisioning',
    approver: 'Jerry Joel',
  },
};
const STAGE_ORDER = [APPROVAL_STAGES.TOWER_HEAD, APPROVAL_STAGES.IT_LICENSE_PROVISIONING];

// Human-readable labels for each approval status value.
const STATUS_LABELS = {
  Pending: 'Pending',
  Approved: 'Approved',
  Rejected: 'Rejected',
  'Not Started': 'Not started',
};

const statusClass = (status) => {
  const normalized = (status || '').toLowerCase().replace(/\s+/g, '-');
  if (normalized === 'approved') return 'approved';
  if (normalized === 'rejected') return 'rejected';
  if (normalized === 'not-started') return 'not-started';
  return 'pending';
};

const statusIcon = (status) => {
  if (status === 'Approved') return <CheckCircle2 size={18} />;
  if (status === 'Rejected') return <XCircle size={18} />;
  return <Clock3 size={18} />;
};

const statusBadgeClass = (status) => {
  if (status === 'Approved') return 'badge-success';
  if (status === 'Rejected') return 'badge-danger';
  if (status === 'Not Started') return 'badge-neutral';
  return 'badge-warning';
};

const DecisionConfirmModal = ({ stage, decision, comments, onCancel, onConfirm, saving }) => {
  const isApprove = decision === 'Approved';
  const stageLabel = STAGE_META[stage]?.label || stage;
  return (
    <Modal
      open
      title={isApprove ? 'Confirm approval' : 'Confirm rejection'}
      onClose={saving ? undefined : onCancel}
      maxWidth={460}
      footer={(
        <div className="flex gap-8" style={{ justifyContent: 'flex-end' }}>
          <button className="btn btn-secondary" onClick={onCancel} disabled={saving}>
            Cancel
          </button>
          <button
            className={`btn ${isApprove ? 'btn-primary' : 'btn-danger'}`}
            onClick={onConfirm}
            disabled={saving}
          >
            {saving ? 'Saving…' : isApprove ? 'Approve' : 'Reject'}
          </button>
        </div>
      )}
    >
      <p style={{ marginTop: 0 }}>
        {isApprove
          ? `Approve this request at the ${stageLabel} stage?`
          : `Reject this request at the ${stageLabel} stage?`}
      </p>
      {comments ? (
        <blockquote className="request-decision-comment-preview">{comments}</blockquote>
      ) : (
        <p className="request-workflow-note">No comment will be recorded.</p>
      )}
    </Modal>
  );
};

const RequestDetailsDrawer = ({ request, onClose, onDecision, stageApprovers = {} }) => {
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [decisionComments, setDecisionComments] = useState('');
  const [savingDecision, setSavingDecision] = useState(false);
  const [pendingDecision, setPendingDecision] = useState(null); // { stage, decision }

  const load = useCallback(async () => {
    setError('');
    try {
      const result = await getLicenseRequest(request.request_id);
      setDetail(result.data);
    } catch (loadError) {
      console.error('Failed to load license request detail:', loadError);
      setError(
        loadError.response?.data?.detail
          || 'Unable to load this request. Check your access and try again.'
      );
    } finally {
      setLoading(false);
    }
  }, [request.request_id]);

  useEffect(() => {
    load();
  }, [load]);

  const openConfirm = (stage, decision) => {
    if (savingDecision) return;
    setError('');
    setSuccess('');
    setPendingDecision({ stage, decision });
  };

  const confirmDecision = async () => {
    if (!pendingDecision || savingDecision) return;
    const { stage, decision } = pendingDecision;
    setSavingDecision(true);
    setError('');
    setSuccess('');
    try {
      const submit =
        stage === APPROVAL_STAGES.IT_LICENSE_PROVISIONING
          ? decideProvisioningStage
          : decideTowerHeadStage;
      const result = await submit(
        request.request_id,
        decision,
        decisionComments.trim() || undefined
      );
      setDetail(result.data);
      setDecisionComments('');
      setPendingDecision(null);
      setSuccess(
        decision === 'Approved'
          ? `${STAGE_META[stage]?.label || 'Stage'} approved successfully.`
          : `${STAGE_META[stage]?.label || 'Stage'} rejected.`
      );
      onDecision?.(result.data);
    } catch (decisionError) {
      console.error('Failed to save approval decision:', decisionError);
      setError(
        decisionError.response?.data?.detail
          || 'The approval decision could not be saved.'
      );
      setPendingDecision(null);
    } finally {
      setSavingDecision(false);
    }
  };

  const approvals = detail?.approvals || [];
  const stageByKey = approvals.reduce((acc, item) => {
    acc[item.stage] = item;
    return acc;
  }, {});

  const renderStageRow = (stageKey) => {
    const meta = STAGE_META[stageKey] || { label: stageKey };
    const approval = stageByKey[stageKey];
    const status = approval?.decision || 'Not Started';
    const canDecide = Boolean(approval?.can_decide) && !detail?.test_mode;
    const decided = status === 'Approved' || status === 'Rejected';
    return (
      <li className={`approval-timeline-item ${statusClass(status)}`} key={stageKey}>
        <span className="approval-timeline-icon">{statusIcon(status)}</span>
        <div>
          <strong>{meta.label}</strong>
          <p>
            {approval?.approver_name
              || stageApprovers[stageKey]?.name
              || meta.approver
              || 'Approver'}
            {' · '}
            {STATUS_LABELS[status] || status}
          </p>
          {approval?.decision_at && <time>Decided {formatDateTime(approval.decision_at)}</time>}
          {approval?.comments && <blockquote>{approval.comments}</blockquote>}
          {canDecide && !decided && (
            <div className="flex gap-8" style={{ marginTop: 10 }}>
              <button
                className="btn btn-primary btn-sm"
                onClick={() => openConfirm(stageKey, 'Approved')}
                disabled={savingDecision}
              >
                <CheckCircle2 size={15} /> Approve
              </button>
              <button
                className="btn btn-danger btn-sm"
                onClick={() => openConfirm(stageKey, 'Rejected')}
                disabled={savingDecision}
              >
                <XCircle size={15} /> Reject
              </button>
            </div>
          )}
        </div>
      </li>
    );
  };

  return (
    <div className="request-drawer-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <aside className="request-drawer" role="dialog" aria-modal="true" aria-labelledby="request-drawer-title">
        <header className="request-drawer-header">
          <div>
            <div className="request-drawer-eyebrow">License request</div>
            <h2 id="request-drawer-title">{request.request_id}</h2>
          </div>
          <button className="topbar-icon-btn" onClick={onClose} aria-label="Close details">
            <X size={18} />
          </button>
        </header>

        <div className="request-drawer-content">
          {loading && <Spinner size={28} />}
          {error && <AlertBanner type="error" message={error} onDismiss={() => setError('')} />}
          {success && <AlertBanner type="success" message={success} onDismiss={() => setSuccess('')} />}
          {!loading && detail && (
            <>
              <section className="request-detail-section">
                <h3><FileText size={16} /> Request Details</h3>
                <dl className="request-detail-grid">
                  <dt>Employee</dt><dd>{detail.employee_name} <span>{detail.employee_email}</span></dd>
                  <dt>Department</dt><dd>{detail.department}</dd>
                  <dt>Application</dt><dd>{detail.application}</dd>
                  <dt>License type</dt><dd>{detail.license_type || detail.license_name || '—'}</dd>
                  <dt>Business justification</dt><dd>{detail.business_justification}</dd>
                  <dt>Submitted</dt><dd>{formatDateTime(detail.requested_at)}</dd>
                  <dt>Current status</dt><dd><span className="badge badge-primary">{detail.status}</span></dd>
                  <dt>Approver</dt><dd>{detail.approver_name || 'No approver assigned'}</dd>
                </dl>
              </section>

              <section className="request-detail-section">
                <h3><ShieldCheck size={16} /> Approval Flow</h3>
                <ol className="approval-timeline">
                  <li className="approval-timeline-item complete">
                    <span className="approval-timeline-icon"><CheckCircle2 size={18} /></span>
                    <div>
                      <strong>Request Submitted</strong>
                      <p>
                        {detail.test_mode
                          ? `Recorded by the shared development account for ${detail.employee_name}.`
                          : `${detail.employee_name} submitted this request.`}
                      </p>
                      <time>{formatDateTime(detail.requested_at)}</time>
                    </div>
                  </li>
                  {detail.test_mode ? (
                    <>
                      <li className="approval-timeline-item pending">
                        <span className="approval-timeline-icon"><Clock3 size={18} /></span>
                        <div>
                          <strong>{STAGE_META[APPROVAL_STAGES.TOWER_HEAD].label}</strong>
                          <p>Pending · Test workflow preview; no approver decision is recorded.</p>
                        </div>
                      </li>
                      <li className="approval-timeline-item not-started">
                        <span className="approval-timeline-icon"><Clock3 size={18} /></span>
                        <div>
                          <strong>{STAGE_META[APPROVAL_STAGES.IT_LICENSE_PROVISIONING].label}</strong>
                          <p>Not started · Activates after Tower Head approval.</p>
                        </div>
                      </li>
                    </>
                  ) : (
                    STAGE_ORDER.map((stageKey) => renderStageRow(stageKey))
                  )}
                </ol>
                {detail.test_mode && (
                  <p className="request-history-empty">
                    The timeline stages above are a test preview, not approval history.
                  </p>
                )}
              </section>

              {!detail.test_mode && (
                <section className="request-detail-section">
                  <h3><UserRound size={16} /> Decision History</h3>
                  {approvals.length ? (
                    <div className="approval-history-list">
                      {approvals.map((item) => (
                        <article className="approval-history-item" key={item.id}>
                          <div>
                            <strong>{item.approver_name}</strong>
                            <span>{item.stage_label || STAGE_META[item.stage]?.label || item.stage}</span>
                          </div>
                          <span className={`badge ${statusBadgeClass(item.decision)}`}>
                            {STATUS_LABELS[item.decision] || item.decision}
                          </span>
                          {item.decision_at && <time>{formatDateTime(item.decision_at)}</time>}
                          {item.comments && <p>{item.comments}</p>}
                        </article>
                      ))}
                    </div>
                  ) : (
                    <p className="request-history-empty">No approval history has been recorded.</p>
                  )}
                </section>
              )}

              {!detail.test_mode && (
                <section className="request-detail-section request-approver-actions">
                  <h3>Decision comment (optional)</h3>
                  <textarea
                    id="approval-comments"
                    className="input"
                    rows={3}
                    placeholder="Add context for your decision…"
                    value={decisionComments}
                    onChange={(event) => setDecisionComments(event.target.value)}
                    disabled={savingDecision}
                  />
                  <p className="request-workflow-note">
                    Use Approve or Reject on the stage above. A confirmation is required before a
                    decision is submitted, and only the configured approver for that stage can decide.
                  </p>
                </section>
              )}
              {!detail.test_mode && detail.status === 'Pending Approval' && (
                <div className="request-approval-readonly">
                  <AlertCircle size={16} />
                  Only the assigned, authenticated approver can decide this stage.
                </div>
              )}
            </>
          )}
        </div>
      </aside>
      {pendingDecision && (
        <DecisionConfirmModal
          stage={pendingDecision.stage}
          decision={pendingDecision.decision}
          comments={decisionComments.trim()}
          saving={savingDecision}
          onCancel={() => (savingDecision ? undefined : setPendingDecision(null))}
          onConfirm={confirmDecision}
        />
      )}
    </div>
  );
};

const LicenseRequestsPage = () => {
  const { user } = useAuth();
  const [licenses, setLicenses] = useState([]);
  const [users, setUsers] = useState([]);
  const [requests, setRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [optionsLoading, setOptionsLoading] = useState(true);
  const [error, setError] = useState('');
  const [optionsError, setOptionsError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');
  const [requestModalOpen, setRequestModalOpen] = useState(false);
  const [selectedRequest, setSelectedRequest] = useState(null);
  const [stageApprovers, setStageApprovers] = useState({});

  const loadRequests = useCallback(async () => {
    setError('');
    try {
      const result = await getLicenseRequests();
      setRequests(Array.isArray(result.data) ? result.data : []);
    } catch (loadError) {
      console.error('Failed to load license requests:', loadError);
      setError(
        loadError.response?.data?.detail
          || 'Unable to load license requests. Verify your access and try again.'
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    getLicenseRequestOptions()
      .then((result) => {
        if (!active) return;
        setUsers(Array.isArray(result.data?.users) ? result.data.users : []);
        setLicenses(Array.isArray(result.data?.licenses) ? result.data.licenses : []);
      })
      .catch((optionsLoadError) => {
        console.error('Failed to load authenticated request options:', optionsLoadError);
        if (active) {
          setOptionsError(
            optionsLoadError.response?.data?.detail
              || 'Unable to load request options for the authenticated employee.'
          );
        }
      })
      .finally(() => {
        if (active) setOptionsLoading(false);
      });

    loadRequests();

    // Best-effort: load the configured stage approvers for display. 404s (no
    // approver configured) simply leave the static fallbacks in place.
    Promise.allSettled([getTowerHeadApprover(), getProvisioningApprover()])
      .then(([towerHead, provisioning]) => {
        if (!active) return;
        setStageApprovers({
          [APPROVAL_STAGES.TOWER_HEAD]:
            towerHead.status === 'fulfilled' ? towerHead.value.data : null,
          [APPROVAL_STAGES.IT_LICENSE_PROVISIONING]:
            provisioning.status === 'fulfilled' ? provisioning.value.data : null,
        });
      })
      .catch((approverLoadError) => {
        console.error('Failed to load configured stage approvers:', approverLoadError);
      });

    return () => {
      active = false;
    };
  }, [loadRequests]);

  const handleSavedRequest = (savedRequest) => {
    setRequests((current) => [
      savedRequest,
      ...current.filter((item) => item.request_id !== savedRequest.request_id),
    ]);
    setSuccessMessage(`Request ${savedRequest.request_id} was saved with status ${savedRequest.status}.`);
    loadRequests();
  };

  const handleDecisionSaved = (updatedRequest) => {
    setRequests((current) => current.map(
      (item) => item.request_id === updatedRequest.request_id ? updatedRequest : item
    ));
  };

  const requestRoles = user?.roles || [];
  const canRequest = user?.license_request_test_mode
    || requestRoles.includes('License.Requester')
    || requestRoles.includes('License.Admin');

  return (
    <>
      <div className="page-header">
        <div>
          <h1 className="page-title">License Requests</h1>
          <p className="page-desc">Submit and track application license requests in one place.</p>
        </div>
        <button
          className="btn btn-primary"
          onClick={() => setRequestModalOpen(true)}
          disabled={optionsLoading || !canRequest}
        >
          <Plus size={16} /> Request License
        </button>
      </div>

      {optionsError && <AlertBanner type="error" message={optionsError} />}
      {error && (
        <AlertBanner
          type="error"
          message={error}
          onDismiss={() => setError('')}
        />
      )}
      {successMessage && (
        <AlertBanner
          type="success"
          message={successMessage}
          onDismiss={() => setSuccessMessage('')}
        />
      )}

      <div className="table-wrapper">
        <table className="table">
          <thead>
            <tr>
              <th>Request ID</th>
              <th>Employee</th>
              <th>Department</th>
              <th>Application</th>
              <th>License Type</th>
              <th>Requested Date</th>
              <th>Approver</th>
              <th>Request Status</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={9} style={{ padding: 24 }}><Spinner size={28} /></td></tr>
            ) : requests.length === 0 ? (
              <tr>
                <td colSpan={9} style={{ padding: 0 }}>
                  <EmptyState
                    icon={<FileText size={24} />}
                    title={error ? 'Requests could not be loaded' : 'No saved license requests'}
                    description={error ? 'Resolve the access or connection issue and retry.' : 'Submitted requests will appear here after the backend confirms they are saved.'}
                    action={error && (
                      <button
                        className="btn btn-secondary btn-sm"
                        onClick={() => {
                          setLoading(true);
                          loadRequests();
                        }}
                      >
                        Retry
                      </button>
                    )}
                  />
                </td>
              </tr>
            ) : requests.map((request) => (
              <tr
                key={request.request_id}
                className="request-table-row"
                onClick={() => setSelectedRequest(request)}
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    setSelectedRequest(request);
                  }
                }}
              >
                <td><button className="request-id-link" onClick={() => setSelectedRequest(request)}>{request.request_id}</button></td>
                <td>{request.employee_name}<small>{request.employee_email}</small></td>
                <td>{request.department}</td>
                <td>{request.application}</td>
                <td>{request.license_type || request.license_name || '—'}</td>
                <td>{formatDateTime(request.requested_at)}</td>
                <td>{request.approver_name || 'Not assigned'}</td>
                <td><span className={`badge ${request.status === 'Rejected' ? 'badge-danger' : request.status === 'Approved' ? 'badge-success' : 'badge-warning'}`}>{request.status}</span></td>
                <td>
                  <button className="btn btn-secondary btn-sm" onClick={(event) => {
                    event.stopPropagation();
                    setSelectedRequest(request);
                  }}>
                    View
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {optionsLoading && <Spinner size={24} />}
      <RequestLicenseModal
        open={requestModalOpen}
        licenses={licenses}
        users={users}
        onClose={() => setRequestModalOpen(false)}
        onSubmitSuccess={handleSavedRequest}
        submitRequest={requestLicense}
      />
      {selectedRequest && (
        <RequestDetailsDrawer
          request={selectedRequest}
          onClose={() => setSelectedRequest(null)}
          onDecision={handleDecisionSaved}
          stageApprovers={stageApprovers}
        />
      )}
    </>
  );
};

export default LicenseRequestsPage;
