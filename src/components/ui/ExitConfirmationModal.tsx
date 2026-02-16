import { Button } from "../ui/button";

type ExitConfirmationModalProps = {
    onClose: () => void;
    onDiscard: () => void;
    onSave: () => void;
};

export function ExitConfirmationModal({
    onClose,
    onDiscard,
    onSave
}: ExitConfirmationModalProps) {
    return (
        <div style={{
            position: 'fixed',
            top: 0, left: 0, right: 0, bottom: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 9999,
            padding: '1rem'
        }}>
            {/* 背景遮罩 */}
            <div
                style={{
                    position: 'absolute',
                    inset: 0,
                    backgroundColor: 'rgba(0,0,0,0.7)',
                    backdropFilter: 'blur(4px)'
                }}
                onClick={onClose}
            />

            {/* 弹窗主体 */}
            <div style={{
                position: 'relative',
                backgroundColor: 'white',
                border: '1px solid #e5e7eb',
                width: '100%',
                maxWidth: '32rem',
                borderRadius: '1rem',
                padding: '1.5rem',
                boxShadow: '0 25px 50px -12px rgba(0,0,0,0.25)',
                color: '#1f2937'
            }}>
                <h3 style={{ fontSize: '1.25rem', fontWeight: 'bold', marginBottom: '0.5rem' }}>Save your plan?</h3>
                <p style={{ color: '#6b7280', marginBottom: '1.5rem' }}>You have unsaved changes. Do you want to save them before leaving?</p>

                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.75rem' }}>
                    <button onClick={onClose} style={{ padding: '0.5rem 1.5rem', borderRadius: '0.75rem', border: '1px solid #ccc' }}>Cancel</button>
                    <button onClick={onDiscard} style={{ padding: '0.5rem 1.5rem', borderRadius: '0.75rem', color: '#ef4444' }}>Discard</button>
                    <button onClick={onSave} style={{ padding: '0.5rem 1.5rem', borderRadius: '0.75rem', backgroundColor: '#2563eb', color: 'white' }}>Save & Exit</button>
                </div>
            </div>
        </div>
    );
}