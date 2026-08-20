# Update the HSN Summary section in app.py (around line 420-470)

        # -------------------------------------------------------------
        # 4.5 HSN Summary Export
        # -------------------------------------------------------------
        st.header("4.5 HSN Summary Reports")
        st.caption(
            "Generate HSN-wise summary reports for B2B and B2C sales. "
            "Only invoices with is_validated = True are included. "
            "Reports include validation to ensure totals match invoice data."
        )
        
        if voucher_type == "Sales":
            if st.button("Generate HSN Summaries", type="primary", key="gen_hsn_btn"):
                summaries = generate_all_hsn_summaries(res.invoices, voucher_type, validate=True)
                st.session_state["hsn_summaries"] = summaries
            
            if st.session_state.get("hsn_summaries") is not None:
                summaries = st.session_state["hsn_summaries"]
                
                for mode, (df, validation_report) in summaries.items():
                    st.subheader(f"{mode} HSN Summary")
                    
                    if df.empty:
                        st.info(f"No {mode} data available")
                        continue
                    
                    # Display validation results
                    if validation_report:
                        col1, col2, col3, col4 = st.columns(4)
                        col1.metric("Total Invoices", validation_report.total_invoices)
                        col2.metric("Invoice Total", f"₹{validation_report.total_invoice_value:,.2f}")
                        col3.metric("HSN Total", f"₹{validation_report.total_hsn_value:,.2f}")
                        col4.metric("Difference", f"₹{validation_report.difference:,.2f}")
                        
                        # Show validation status
                        if validation_report.is_valid:
                            st.success(f"✅ Validation PASSED: HSN totals match invoice totals")
                        else:
                            st.error(f"❌ Validation FAILED: HSN totals do not match invoice totals (difference: ₹{validation_report.difference:,.2f})")
                        
                        # Show details of mismatched invoices
                        if validation_report.mismatched_invoices:
                            with st.expander(f"⚠️ Mismatched invoices ({len(validation_report.mismatched_invoices)})"):
                                st.dataframe(
                                    pd.DataFrame(validation_report.mismatched_invoices),
                                    use_container_width=True,
                                    hide_index=True
                                )
                        
                        if validation_report.missing_hsn_invoices:
                            with st.expander(f"⚠️ Invoices without HSN codes ({len(validation_report.missing_hsn_invoices)})"):
                                st.dataframe(
                                    pd.DataFrame(validation_report.missing_hsn_invoices),
                                    use_container_width=True,
                                    hide_index=True
                                )
                    
                    # Display metrics
                    total_value = df["Total Value"].sum()
                    total_taxable = df["Taxable Value"].sum()
                    total_tax = df["Integrated Tax Amount"].sum() + df["Central Tax Amount"].sum() + df["State/UT Tax Amount"].sum()
                    
                    c1, c2, c3 = st.columns(3)
                    c1.metric(f"{mode} - Total Value", f"₹{total_value:,.2f}")
                    c2.metric(f"{mode} - Taxable Value", f"₹{total_taxable:,.2f}")
                    c3.metric(f"{mode} - Total Tax", f"₹{total_tax:,.2f}")
                    
                    # Show the table
                    st.dataframe(df, use_container_width=True, hide_index=True)
                    
                    # Download buttons
                    col1, col2 = st.columns(2)
                    
                    csv = df.to_csv(index=False)
                    col1.download_button(
                        f"Download {mode} HSN Summary (.csv)",
                        data=csv,
                        file_name=f"hsn_{mode.lower()}.csv",
                        mime="text/csv",
                        key=f"dl_hsn_{mode}",
                    )
                    
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                        df.to_excel(writer, sheet_name=f"HSN_{mode}", index=False)
                    col2.download_button(
                        f"Download {mode} HSN Summary (.xlsx)",
                        data=buf.getvalue(),
                        file_name=f"hsn_{mode.lower()}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"dl_hsn_excel_{mode}",
                    )
        else:
            st.info(f"HSN summary generation is currently only implemented for Sales vouchers. Voucher type: {voucher_type}")
