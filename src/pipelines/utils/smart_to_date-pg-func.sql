CREATE OR REPLACE FUNCTION smart_to_date(input_text TEXT) 
RETURNS DATE AS $$
DECLARE
    clean_str TEXT;
    result_date DATE;
BEGIN
    -- 1. Pre-cleaning: Lowercase, remove $, ^, and extra whitespace
    clean_str := LOWER(TRIM(input_text));
    clean_str := REGEXP_REPLACE(clean_str, '[\$\^]', '', 'g'); -- Remove symbols
    clean_str := REGEXP_REPLACE(clean_str, '\s+', ' ', 'g');   -- Normalize spaces
    
    -- Fix specific typos or language issues found in your list
    clean_str := REPLACE(clean_str, 'janaury', 'january');
    clean_str := REPLACE(clean_str, 'märz', 'march');
    
    -- 2. Try Standard Formats
    BEGIN
        -- Try DD Month YYYY (e.g., 20 February 2003)
        RETURN TO_DATE(clean_str, 'DD Month YYYY');
    EXCEPTION WHEN OTHERS THEN 
        -- Fall through to next attempt
    END;

    BEGIN
        -- Try Month DD, YYYY (e.g., January 1, 1991)
        RETURN TO_DATE(REGEXP_REPLACE(clean_str, ',', '', 'g'), 'Month DD YYYY');
    EXCEPTION WHEN OTHERS THEN END;

    BEGIN
        -- Try DD.MM.YYYY or DD/MM/YYYY
        IF clean_str ~ '^\d{1,2}[\./]\d{1,2}[\./]\d{2,4}$' THEN
            RETURN TO_DATE(clean_str, 'DD MM YYYY');
        END IF;
    EXCEPTION WHEN OTHERS THEN END;

    -- 3. Handle "Jammed" Text (e.g., 14May1971 or 9June1998)
    BEGIN
        IF clean_str ~ '^\d{1,2}[a-z]+\d{4}$' THEN
            RETURN TO_DATE(clean_str, 'DDMonthYYYY');
        END IF;
    EXCEPTION WHEN OTHERS THEN END;

    -- 4. Handle Year Only or Approximations (e.g., At the beginning of 1970)
    -- This extracts the first 4-digit number found
    BEGIN
        IF clean_str ~ '\d{4}' THEN
            RETURN TO_DATE(SUBSTRING(clean_str FROM '\d{4}'), 'YYYY');
        END IF;
    EXCEPTION WHEN OTHERS THEN END;

    -- Return NULL if all parsing attempts fail
    RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE;