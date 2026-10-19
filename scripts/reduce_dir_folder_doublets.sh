cd /tmp/docs/

for dir in */; do
    # Check if the nested subdirectory exists to avoid errors
    if [ -d "${dir}${dir}" ]; then
        # Move everything from inside the nested dir to the current dir
        mv "${dir}${dir}"* "$dir"
        
        # Remove the now-empty nested directory
        rmdir "${dir}${dir}"
    fi
done
